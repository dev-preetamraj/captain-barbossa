"""The native CLI's hook event stream: append one record, and hand a crew its mail.

Claude `--settings` hooks and Codex `notify` run `captain hook`, which lands here.
Nothing here touches the graph.
"""

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import lifetime, native_history
from .approval import Decision, Request, Verdict, decide, denied
from .eventlog import DELIVERY_HOOKS, event_incarnation, event_line, match_crew
from .runtime import CaptainError
from .sessions import Session
from .store import read_json


def crew_for_events(events_path):
    """(session directory, crew id, active assignment) for the crew this hook belongs to.

    A hook knows only its own events path; this is the bridge back into protocol state.
    """
    directory = events_path.parent.parent
    state = read_json(directory / "protocol.json")
    crew_id, suffix = match_crew(events_path.name.removesuffix(".jsonl"), state["active"])
    if crew_id is None:
        raise CaptainError("Unknown crew event path.")
    assignment = state["assignments"].get(state["active"][crew_id])
    if assignment and suffix != assignment["incarnation_id"]:
        raise CaptainError("Stale crew incarnation.")
    return directory, crew_id, assignment


def mail_context(events_path):
    """This crew's rendered mail (or None), and the exact message IDs it contains.

    Flush this before stamping the receipt (`receipt_for`), or a dropped hook output
    marks mail read that no model saw.
    """
    from .protocol import queued, render

    directory, crew_id, _ = crew_for_events(events_path)
    messages = queued(directory, crew_id)
    return render(directory, messages), [message["id"] for message in messages]


def receipt_for(events_path, message_ids):
    """Stamp the receipt for whatever mail_context just rendered and flushed."""
    from .protocol import receipt

    directory, crew_id, _ = crew_for_events(events_path)
    receipt(directory, crew_id, message_ids)


def stop_reason(events_path):
    """Why this crew may not end its turn yet, or None. A crew awaiting an answer to ask may
    stop; instruction wording cannot enforce either rule, only the Stop hook can."""
    _, crew_id, assignment = crew_for_events(events_path)
    if assignment and assignment["state"] != "done" and not assignment["question"]:
        name = assignment["crew"]
        return f"Your assignment is unfinished; finish with `captain done {name} --report '...'`."
    return None


def _incarnation_crew(directory, incarnation, meta=None):
    meta = meta or read_json(directory / "session.json")
    crew_id = next(
        key for key, value in meta["crew"].items() if value.get("incarnation_id") == incarnation
    )
    return crew_id, meta["crew"][crew_id], Session(directory, meta)


def plugin_denial(events_path, event, meta=None):
    """A plugin-denied decision for a crew request whose command a plugin forbids, or None.

    Session denials are fixed at captain launch and a crew's own at its recruit; both apply,
    so relaunching the captain without plugins never frees a crew already running.
    """
    path = Path(events_path)
    metadata = path.parent.parent / "session.json"
    if path.stem == "captain" or (meta is None and not metadata.exists()):
        return None
    meta = meta or read_json(metadata)
    roster = meta.get("crew", {})
    record = roster.get(match_crew(path.stem, roster)[0]) or {}
    denials = [
        *(meta.get("plugins") or {}).get("denials", []),
        *(record.get("plugins") or {}).get("denials", []),
    ]
    inputs = event.get("tool_input")
    command = inputs.get("command") if isinstance(inputs, dict) else None
    return Decision("plugin-denied") if denied(denials, command) else None


def permission_decision(events_path, event):
    """Bind one native tool request to this incarnation's active assignment grants."""
    try:
        meta = read_json(events_path.parent.parent / "session.json")
        if refusal := plugin_denial(events_path, event, meta):
            return refusal
        directory, _, assignment = crew_for_events(events_path)
        # Local, like this module's other upper imports: each hook is a fresh process.
        from .execution import root

        _, record, current = _incarnation_crew(directory, assignment["incarnation_id"], meta)
        if lifetime.revoked(current, record):
            return Decision("action-not-granted")
        project = str(root(current, record))
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
    """Find an unconsumed decision for this exact native call and execution context."""
    identity = ("session_id", "tool_use_id", "cwd", "incarnation_id")
    if not all(event.get(key) for key in identity[:3]):
        return None
    answered = {
        r["approval"].get("pretool") for r in records if isinstance(r.get("approval"), dict)
    }
    for record in reversed(records):
        approval = record.get("approval")
        if not isinstance(approval, dict) or approval.get("source") != "PreToolUse":
            continue
        if record.get("tool_use_id") in answered or any(
            record.get(key) != event.get(key) for key in identity
        ):
            continue
        if (record.get("tool_name"), record.get("tool_input")) == (
            event.get("tool_name"),
            event.get("tool_input"),
        ):
            return record
    return None


def permission_output(decision, hook="PermissionRequest"):
    if decision.verdict == Verdict.DEFER:
        return None
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


def environment_events():
    """Resolve the stable Codex command against its launch-bound identity."""
    from .sessions import read_session

    current = read_session(Path(os.environ["CAPTAIN_PROJECT"]), os.environ["CAPTAIN_SESSION"])
    name = os.environ.get("CAPTAIN_CREW")
    if not name:
        if os.environ.get("CAPTAIN_ROLE") == "crew":
            raise CaptainError("Missing crew hook identity.")
        return current.events("captain")
    crew_id = name.lower()
    record = current.meta["crew"][crew_id]
    incarnation = os.environ["CAPTAIN_INCARNATION"]
    if not incarnation or record.get("incarnation_id") != incarnation:
        raise CaptainError("Stale crew hook identity.")
    return current.events(f"{crew_id}-{incarnation}")


def append_event(args=None):
    """Native hooks supply JSON on stdin (Claude) or as the last argument (Codex).

    `args` is the events path then the optional JSON; None reads sys.argv for the
    pre-0.30 `-c` hook that old sessions still run.
    """
    args = sys.argv[1:] if args is None else args
    events_path = args[0]
    if events_path == "--environment":
        try:
            events_path = environment_events()
        except (CaptainError, OSError, ValueError, KeyError, TypeError):
            return
    event = json.loads(args[1]) if len(args) > 1 else json.load(sys.stdin)
    if not isinstance(event, dict):
        return
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    try:
        incarnation = event_incarnation(Path(events_path))
    except CaptainError:
        return
    if incarnation and event.get("incarnation_id", incarnation) != incarnation:
        return
    provider = "claude" if "hook_event_name" in event else "codex"
    managed_provider = bool(incarnation)
    if Path(events_path).stem == "captain":
        metadata = Path(events_path).parent.parent / "session.json"
        if metadata.exists():
            captain_provider = read_json(metadata).get("captain_provider")
            if captain_provider:
                provider, managed_provider = captain_provider, True
    if incarnation:
        # Local, like this module's other upper imports: each hook is a fresh process.
        from .crew import Crew

        directory = Path(events_path).parent.parent
        crew = Crew(*_incarnation_crew(directory, incarnation))
        provider = crew.record.get("provider", provider)
    if managed_provider:
        event["provider"] = provider
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
        kind = event.get("hook_event_name") if provider == "claude" else None
        if kind == "PreToolUse":
            permission = permission_decision(Path(events_path), event)
            if permission.code == "unknown-tool":
                permission = None  # Keep activity evidence without changing native permissions.
        prior = pretool_decision(records, event) if kind == "PermissionRequest" else None
        if kind == "PermissionRequest":
            # Grants, lifetime and filesystem paths may have changed since PreToolUse.
            permission = permission_decision(Path(events_path), event)
        if prior and permission.code != prior["approval"]["code"]:
            prior = None
        if prior and permission.verdict in (Verdict.ESCALATE, Verdict.DEFER):
            # The PreToolUse ask already carries the reason; this record is the pending prompt.
            approval = {key: prior["approval"][key] for key in ("verdict", "code")}
            event["approval"] = {**approval, "pretool": prior.get("tool_use_id")}
            permission = None
        elif kind == "PermissionRequest" or (kind == "PreToolUse" and permission is not None):
            event["approval"] = {"verdict": permission.verdict.value, "code": permission.code}
            if prior:
                event["approval"]["pretool"] = prior["tool_use_id"]
            if kind == "PreToolUse":
                event["approval"]["source"] = kind
            # A deferred PermissionRequest is the native mode's own prompt, still pending.
            if permission.verdict != Verdict.ESCALATE and (
                kind == "PreToolUse" or permission.verdict != Verdict.DEFER
            ):
                # Resolved requests are audit evidence, not a pending approval for wait.
                event["hook_event_name"] = "PermissionDecision"
                event["status"] = (
                    f"working: approval {permission.verdict.value} [{permission.code}]"
                )
        file.write(event_line(event))
        file.flush()
    if incarnation:
        try:
            native_history.observe(crew, event)
        except (CaptainError, OSError, ValueError, TypeError) as exc:
            print(
                f"captain: native history observation retained for review: {exc}", file=sys.stderr
            )
    if permission is not None:
        output = permission_output(permission, kind)
        if output:
            print(json.dumps(output))
            sys.stdout.flush()
        return
    if provider == "codex" and event.get("hook_event_name") == "Stop":
        # Codex Stop accepts decision/reason, not Claude's mail additionalContext.
        output = {}
        if not event.get("stop_hook_active"):
            try:
                reason = stop_reason(Path(events_path))
                if reason:
                    output = {"decision": "block", "reason": reason}
            except (CaptainError, OSError, ValueError, KeyError, StopIteration):
                pass
        print(json.dumps(output))
        sys.stdout.flush()
        return
    # stop_hook_active means a block already fired; blocking again would loop the crew.
    if kind not in DELIVERY_HOOKS or (kind == "Stop" and event.get("stop_hook_active")):
        return
    try:
        deliver_mail(Path(events_path), kind)
    except (CaptainError, OSError, ValueError, KeyError, StopIteration):
        return  # a hook that cannot read state must never block or wedge the crew


def deliver_mail(events_path, hook):
    """Hand this crew its queued mail, then stamp the receipt once the body is flushed."""
    body, message_ids = mail_context(events_path)
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
    receipt_for(events_path, message_ids)
