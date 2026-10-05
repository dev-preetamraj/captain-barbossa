"""The native CLI's hook event stream: append one record, and hand a crew its mail.

Claude `--settings` hooks and Codex `notify` run `captain hook`, which lands here.
Nothing here touches the graph.
"""

import fcntl
import json
import os
import sys
from pathlib import Path

from .runtime import CaptainError
from .store import read_json


def crew_for_events(events_path):
    """(session directory, crew id, active assignment) for the crew this hook belongs to.

    A hook knows only its own events path; this is the bridge back into protocol state.
    """
    directory = events_path.parent.parent
    state = read_json(directory / "protocol.json")
    stem = events_path.name.removesuffix(".jsonl")
    crew_id = next(c for c in state["active"] if stem == c or stem.startswith(f"{c}-"))
    return directory, crew_id, state["assignments"].get(state["active"][crew_id])


def mail_context(events_path):
    """This crew's queued mail, rendered for its own model, or None.

    Flush this before stamping the receipt (`receipt_for`), or a dropped hook output
    marks mail read that no model saw.
    """
    from .protocol import queued, render

    directory, crew_id, _ = crew_for_events(events_path)
    return render(directory, queued(directory, crew_id))


def receipt_for(events_path):
    """Stamp the receipt for whatever mail_context just rendered and flushed."""
    from .protocol import queued, receipt

    directory, crew_id, _ = crew_for_events(events_path)
    receipt(directory, crew_id, [message["id"] for message in queued(directory, crew_id)])


def stop_reason(events_path):
    """Why this crew may not end its turn yet, or None. A crew awaiting an answer to ask may
    stop; instruction wording cannot enforce either rule, only the Stop hook can."""
    _, crew_id, assignment = crew_for_events(events_path)
    if assignment and assignment["state"] != "done" and not assignment["question"]:
        name = assignment["crew"]
        return f"Your assignment is unfinished; finish with `captain done {name} --report '...'`."
    return None


def append_event(args=None):
    """Native hooks supply JSON on stdin (Claude) or as the last argument (Codex).

    `args` is the events path then the optional JSON; None reads sys.argv for the
    pre-0.30 `-c` hook that old sessions still run.
    """
    args = sys.argv[1:] if args is None else args
    events_path = args[0]
    event = json.loads(args[1]) if len(args) > 1 else json.load(sys.stdin)
    if not isinstance(event, dict):
        return
    # O_NOFOLLOW rejects a symlink swapped in at this path; append never clobbers existing data.
    fd = os.open(events_path, os.O_NOFOLLOW | os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
        file.flush()
    # Only Claude sends hook_event_name, so Codex notify and pi are untouched. stop_hook_active
    # means a block already fired; blocking again would loop the crew.
    hook = event.get("hook_event_name")
    if hook not in DELIVERY_HOOKS or (hook == "Stop" and event.get("stop_hook_active")):
        return
    try:
        deliver_mail(Path(events_path), hook)
    except (CaptainError, OSError, ValueError, KeyError, StopIteration):
        return  # a hook that cannot read state must never block or wedge the crew


# The hooks that hand a crew its mail, in its own process.
DELIVERY_HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")


def deliver_mail(events_path, hook):
    """Hand this crew its queued mail, then stamp the receipt once the body is flushed."""
    body = mail_context(events_path)
    if body is None:
        reason = stop_reason(events_path) if hook == "Stop" else None
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
        return
    payload = {"hookSpecificOutput": {"hookEventName": hook, "additionalContext": body}}
    if hook == "Stop":
        # The body rides additionalContext; the block only holds the turn open.
        payload["decision"] = "block"
        payload["reason"] = "Mail from the captain arrived; act on it before finishing."
    print(json.dumps(payload))
    sys.stdout.flush()
    # Only after the flush: at-least-once, since a duplicate beats a silent loss.
    receipt_for(events_path)


def read_events(path, offset):
    """Read complete JSONL records, retaining an unfinished final line for the next poll."""
    events = []
    try:
        with path.open("rb") as file:
            file.seek(offset)
            while line := file.readline():
                if not line.endswith(b"\n"):
                    break
                offset = file.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except FileNotFoundError:
        pass
    return events, offset
