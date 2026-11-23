"""Native hook event files: bounded lines, incarnation stamps and offset reads."""

import json

from .runtime import CaptainError
from .store import read_json


def match_crew(stem, crews):
    """(crew id, incarnation suffix or None) for a hook file stem, matching whole ids only.

    Incarnations are hex without dashes, so `jack-1-<hex>` splits on its last dash and
    cannot be taken for crew `jack`, even when `jack` also exists.
    """
    if stem in crews:
        return stem, None
    head, sep, tail = stem.rpartition("-")
    if sep and head in crews:
        return head, tail
    return None, None


def event_incarnation(path):
    """Bind a managed hook path to the current roster, including before assignment."""
    metadata = path.parent.parent / "session.json"
    if path.stem == "captain" or not metadata.exists():
        return None
    roster = read_json(metadata).get("crew", {})
    crew_id, suffix = match_crew(path.stem, roster)
    if crew_id is None:
        raise CaptainError("Unknown crew event path.")
    incarnation = roster[crew_id].get("incarnation_id")
    if incarnation and suffix != incarnation:
        raise CaptainError("Stale crew incarnation.")
    return incarnation


# The hooks that hand a crew its mail, in its own process.
DELIVERY_HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")


MAX_EVENT_BYTES = 65536


def event_line(event):
    """Bound persisted payloads, never the identity or the live permission request."""
    record = dict(event)
    protected = {
        "hook_event_name",
        "type",
        "notification_type",
        "incarnation_id",
        "seq",
        "session_id",
        "thread_id",
        "thread-id",
        "turn_id",
        "turn-id",
        "tool_use_id",
        "tool_name",
        "timestamp",
        "provider",
        "cwd",
        "approval",
        "status",
        "transcript_path",
        "source",
        "reason",
        "payload_truncated",
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    if len(line.encode("utf-8")) <= MAX_EVENT_BYTES:
        return line
    record["payload_truncated"] = True
    fields = sorted(
        (key for key in record if key not in protected),
        key=lambda key: len(json.dumps(record[key], ensure_ascii=False).encode("utf-8")),
        reverse=True,
    )
    for key in fields:
        del record[key]
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if len(line.encode("utf-8")) <= MAX_EVENT_BYTES:
            return line
    raise CaptainError("Native event identity exceeds 64 KiB.")


def read_events(path, offset, *, incarnation=None, max_bytes=None):
    """Read complete records; bounded polls skip legacy oversized lines incrementally."""
    if max_bytes is not None and (max_bytes <= 0 or incarnation):
        raise ValueError("Bounded reads require a positive budget and a byte-offset cursor.")
    events = []
    seq = 0
    start = offset
    try:
        with path.open("rb") as file:
            skipping = False
            if max_bytes is not None and offset:
                file.seek(offset - 1)
                skipping = file.read(1) != b"\n"
            file.seek(0 if incarnation else offset)
            remaining = max_bytes
            while remaining is None or remaining > 0:
                line = file.readline(-1 if remaining is None else remaining)
                if not line:
                    break
                if remaining is not None:
                    remaining -= len(line)
                complete = line.endswith(b"\n")
                if skipping:
                    offset = file.tell()
                    skipping = not complete
                    continue
                if not complete:
                    # A short partial line may still finish; a full budget cannot fit.
                    if max_bytes is not None and len(line) == max_bytes:
                        offset = file.tell()
                    break
                offset = file.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    if incarnation:
                        value = event.get("seq")
                        if event.get("incarnation_id") != incarnation:
                            continue
                        if type(value) is not int or value <= seq:
                            continue
                        seq = value
                    if offset > start:
                        events.append(event)
    except FileNotFoundError:
        pass
    return events, offset
