"""Event files: native hook lines, a supervised session's protocol actions, bounded lines,
incarnation stamps and offset reads."""

import json
import os
import re
import time
from uuid import uuid4

from .runtime import CaptainError
from .store import lock, private_dir, read_json, write_text


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
# A supervised session's protocol actions, one line each, read by byte offset like hook events.
ACTIONS = "actions.jsonl"
# Actions waiting for the log, each a file named so that sorting keeps their order.
ACTION_SPOOL = "actions.spool"
ACTION_LOCK_WAIT = 1.0
# The token a supervisor hands its app; the action log and every reply redact it.
TOKEN_ENV = "CAPTAIN_SUPERVISOR_TOKEN"
ACTION_KINDS = (
    "recruit",
    "assign",
    "mail",
    "question",
    "grant",
    "report",
    "result",
    "lifecycle",
    "authorization",
    "review",
    "prompt",
    "session-renamed",
)
ACTION_IDENTITY = ("id", "kind", "at", "crew", "assignment", "message", "question", "request")
# Key suffixes, compared without case, dashes or underscores, whose values never leave in clear.
SECRET_KEYS = ("apikey", "secret", "password", "authorization", "token")
REDACTED = "[redacted]"
# A quoted secret-named key and its colon; the value after it is one JSON value of any type.
SECRET_KEY = re.compile(
    r'"[^"\\]*?(?:api[-_]?key|secret|password|authorization|token)"\s*:\s*', re.IGNORECASE
)
QUOTED = re.compile(r'"(?:[^"\\]|\\.)*(?:"|$)')
BARE = re.compile(r"[^,}\]\s]*")
_decoder = json.JSONDecoder()
# Literal secrets this process knows besides the token in its environment: a supervisor's own.
_hidden = set()


def hide(secret):
    _hidden.add(secret)


def known_secrets():
    return tuple(filter(None, {*_hidden, os.environ.get(TOKEN_ENV)}))


def redact_text(text, literals=()):
    """Serialized text with literal secrets and every secret-named key's value redacted."""
    for secret in literals:
        if secret:
            text = text.replace(secret, REDACTED)
    parts, at = [], 0
    for match in SECRET_KEY.finditer(text):
        if match.start() < at:
            continue  # inside a value already redacted
        parts += [text[at : match.end()], f'"{REDACTED}"']
        at = _value_end(text, match.end())
    return "".join(parts) + text[at:]


def _value_end(text, start):
    """Where the JSON value at `start` ends; one cut off by the end of the text runs to it."""
    try:
        return _decoder.raw_decode(text, start)[1]
    except ValueError:
        pass
    if text.startswith('"', start):
        return QUOTED.match(text, start).end()
    if text.startswith(("[", "{"), start):
        return len(text)
    return BARE.match(text, start).end()


def redact(value, secrets=()):
    """A copy with secret-named keys and every literal in `secrets` replaced by REDACTED."""
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str)
            and key.casefold().replace("-", "").replace("_", "").endswith(SECRET_KEYS)
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def record_action(directory, kind, crew, assignment, *, supervised, defer=False, **fields):
    """Log one protocol action with a stable id when `supervised`; spools when `defer` or busy."""
    if kind not in ACTION_KINDS:
        raise ValueError(f"Unknown protocol action kind: {kind}")
    if not supervised:
        return None
    action = {
        "id": uuid4().hex,
        "kind": kind,
        "at": time.time(),
        "crew": crew,
        "assignment": assignment,
        **fields,
    }
    try:
        line = event_line(redact(action, known_secrets()), ACTION_IDENTITY)
    except CaptainError:
        return None
    try:
        if defer:
            raise BlockingIOError
        flush_actions(directory, line)
    except (CaptainError, OSError):
        try:
            spool = private_dir(directory / ACTION_SPOOL)
            write_text(spool / f"{time.time_ns():020d}-{action['id']}.jsonl", line)
        except (CaptainError, OSError):
            return None  # the protocol change stands; only its log line is lost
    return action["id"]


def flush_actions(directory, line=None):
    """Append spooled actions, then `line`, under a bounded lock; a crash may repeat an id."""
    spool = directory / ACTION_SPOOL
    with lock(directory / "actions.lock", timeout=ACTION_LOCK_WAIT):
        pending = sorted(spool.glob("*.jsonl")) if spool.is_dir() else []
        if not pending and line is None:
            return
        flags = os.O_NOFOLLOW | os.O_APPEND | os.O_CREAT | os.O_WRONLY
        with os.fdopen(os.open(directory / ACTIONS, flags, 0o600), "a", encoding="utf-8") as file:
            for path in pending:
                file.write(path.read_text(encoding="utf-8"))
                file.flush()
                path.unlink()
            if line is not None:
                file.write(line)


def event_line(event, keep=()):
    """Bound persisted payloads, never the identity or the live permission request."""
    record = dict(event)
    protected = {
        *keep,
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
