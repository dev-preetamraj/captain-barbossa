"""The native CLI's hook event stream: append one record, and hand a crew its mail.

Claude `--settings` hooks and Codex `notify` run `captain hook`, which lands here.
Nothing here touches the graph.
"""

import fcntl
import json
import os
import sys
from pathlib import Path

from .approval import Decision, Request, Verdict, decide
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
    assignment = state["assignments"].get(state["active"][crew_id])
    if assignment and stem != f"{crew_id}-{assignment['incarnation_id']}":
        raise CaptainError("Stale crew incarnation.")
    return directory, crew_id, assignment


def event_incarnation(path):
    """Bind a managed hook path to the current roster, including before assignment."""
    metadata = path.parent.parent / "session.json"
    if path.stem == "captain" or not metadata.exists():
        return None
    roster = read_json(metadata).get("crew", {})
    for crew_id, record in roster.items():
        if path.stem == crew_id or path.stem.startswith(f"{crew_id}-"):
            incarnation = record.get("incarnation_id")
            if incarnation and path.stem != f"{crew_id}-{incarnation}":
                raise CaptainError("Stale crew incarnation.")
            return incarnation
    raise CaptainError("Unknown crew event path.")


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


def permission_decision(events_path, event):
    """Bind one native tool request to this incarnation's active assignment grants."""
    try:
        directory, _, assignment = crew_for_events(events_path)
        project = read_json(directory / "session.json")["project"]
        cwd, tool, inputs = event["cwd"], event["tool_name"], event["tool_input"]
        if not all(isinstance(value, str) and value for value in (project, cwd, tool)):
            return Decision("unparsed")
        if not isinstance(inputs, dict) or not Path(cwd).is_absolute():
            return Decision("unparsed")
        paths = tuple(
            inputs[key] for key in ("file_path", "notebook_path", "path") if key in inputs
        )
        command = inputs.get("command")
        if any(not isinstance(path, str) or not path for path in paths):
            return Decision("unparsed")
        if command is not None and not isinstance(command, str):
            return Decision("unparsed")
        if not assignment or assignment["state"] == "done":
            return Decision("action-not-granted")
        return decide(
            Request(
                tool=tool,
                project=str(Path(project).resolve()),
                cwd=str(Path(cwd).resolve()),
                owned=tuple(assignment["paths"]),
                actions=tuple(assignment["actions"]),
                command=command,
                paths=tuple(str((Path(cwd) / path).resolve()) for path in paths),
            )
        )
    except (CaptainError, OSError, ValueError, KeyError, StopIteration, TypeError):
        return Decision("unparsed")


def pretool_decision(records, event):
    """The PreToolUse decision this PermissionRequest prompts for, so one call is decided once."""
    answered = {
        r["approval"].get("pretool") for r in records if isinstance(r.get("approval"), dict)
    }
    for record in reversed(records):
        approval = record.get("approval")
        if not isinstance(approval, dict) or approval.get("source") != "PreToolUse":
            continue
        if record.get("tool_use_id") in answered:
            continue
        if (record.get("tool_name"), record.get("tool_input")) == (
            event.get("tool_name"),
            event.get("tool_input"),
        ):
            return record
    return None


def permission_output(decision, hook="PermissionRequest"):
    message = f"Captain approval [{decision.code}]: {decision.reason}"
    if hook == "PreToolUse":
        verdicts = {Verdict.ALLOW_ONCE: "allow", Verdict.DENY: "deny", Verdict.ESCALATE: "ask"}
        native = {"hookEventName": hook, "permissionDecision": verdicts[decision.verdict]}
        if decision.verdict != Verdict.ALLOW_ONCE:
            native["permissionDecisionReason"] = message
        return {"hookSpecificOutput": native}
    if decision.verdict == Verdict.ESCALATE:
        return {"systemMessage": f"{message}. User approval required; review the native prompt."}
    native = {"behavior": "allow" if decision.verdict == Verdict.ALLOW_ONCE else "deny"}
    if decision.verdict == Verdict.DENY:
        native["message"] = message
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": native}}


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
    try:
        incarnation = event_incarnation(Path(events_path))
    except CaptainError:
        return
    if incarnation and event.get("incarnation_id", incarnation) != incarnation:
        return
    permission = None
    # O_NOFOLLOW rejects a symlink swapped in at this path; append never clobbers existing data.
    fd = os.open(events_path, os.O_NOFOLLOW | os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        records = []
        for line in file:
            try:
                previous = json.loads(line)
            except ValueError:
                continue
            if isinstance(previous, dict) and previous.get("incarnation_id") == incarnation:
                records.append(previous)
        if incarnation:
            try:
                current = event_incarnation(Path(events_path))
            except CaptainError:
                return
            if current != incarnation:
                return
            seq = max((r["seq"] for r in records if type(r.get("seq")) is int), default=0)
            if "seq" in event and (type(event["seq"]) is not int or event["seq"] <= seq):
                return
            event["incarnation_id"] = incarnation
            event["seq"] = event.get("seq", seq + 1)
        kind = event.get("hook_event_name")
        if kind == "PreToolUse":
            permission = permission_decision(Path(events_path), event)
            if permission.code == "unknown-tool":
                return  # no policy, so the native permission flow decides alone
        prior = pretool_decision(records, event) if kind == "PermissionRequest" else None
        if prior:
            permission = Decision(prior["approval"]["code"])
            if permission.verdict != Verdict.ESCALATE:
                # Already decided and logged; a native ask rule re-prompted the same call.
                print(json.dumps(permission_output(permission)))
                return
            # The PreToolUse ask already carries the reason; this record is the pending prompt.
            approval = {key: prior["approval"][key] for key in ("verdict", "code")}
            event["approval"] = {**approval, "pretool": prior.get("tool_use_id")}
            permission = None
        elif kind in ("PreToolUse", "PermissionRequest"):
            if kind == "PermissionRequest":
                permission = permission_decision(Path(events_path), event)
            event["approval"] = {"verdict": permission.verdict.value, "code": permission.code}
            if kind == "PreToolUse":
                event["approval"]["source"] = kind
            if permission.verdict != Verdict.ESCALATE:
                # Resolved requests are audit evidence, not a pending approval for wait.
                event["hook_event_name"] = "PermissionDecision"
                event["status"] = (
                    f"working: approval {permission.verdict.value} [{permission.code}]"
                )
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
        file.flush()
    if permission is not None:
        print(json.dumps(permission_output(permission, kind)))
        sys.stdout.flush()
        return
    # Only Claude sends hook_event_name, so Codex notify and pi are untouched. stop_hook_active
    # means a block already fired; blocking again would loop the crew.
    if kind not in DELIVERY_HOOKS or (kind == "Stop" and event.get("stop_hook_active")):
        return
    try:
        deliver_mail(Path(events_path), kind)
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


def read_events(path, offset, *, incarnation=None):
    """Read complete JSONL records, retaining an unfinished final line for the next poll."""
    events = []
    seq = 0
    start = offset
    try:
        with path.open("rb") as file:
            file.seek(0 if incarnation else offset)
            while line := file.readline():
                if not line.endswith(b"\n"):
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
