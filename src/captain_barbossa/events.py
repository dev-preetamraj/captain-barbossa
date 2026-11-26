"""The native CLI's hook event stream: append one record, and hand a crew its mail.

Claude `--settings` hooks, Codex hooks and `notify`, and pi's managed extension run
`captain hook`, which lands here. Nothing here touches the graph.
"""

import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import lifetime, native_history
from .approval import (
    EDIT_TOOLS,
    FLAG,
    FLAGGED,
    SHELL_TOOLS,
    Decision,
    Request,
    Verdict,
    command_text,
    decide,
    denied,
    protected,
)
from .eventlog import DELIVERY_HOOKS, event_incarnation, event_line, match_crew
from .runtime import CaptainError
from .sessions import Session
from .store import lock, read_json, state_storage

# The gate hooks every gated provider sends in Claude's wire shape. Codex's SessionStart
# and Stop keep their own handling: Codex mail rides the ring, never a hook.
GATE_HOOKS = ("PreToolUse", "PermissionRequest")
GATED_PROVIDERS = ("claude", "codex", "pi")
# Providers whose hooks report each call's outcome; the rest settle a creation lazily.
POST_TOOL_PROVIDERS = ("claude",)
POST_TOOL_HOOKS = ("PostToolUse", "PostToolUseFailure")
# pi's managed extension forwards its lowercase built-in tool names (pi docs, extensions.md
# "tool_call"; dist/core/tools/*.d.ts schemas: bash{command}, read/edit/write{path}; the
# optional powershell{command} is a shell too, dist/core/tools/index.d.ts ToolName).
PI_TOOLS = {
    "bash": "Bash", "powershell": "Bash", "read": "Read", "edit": "Edit", "write": "Write",
    "grep": "Grep", "find": "Glob", "ls": "LS",
}  # fmt: skip
# pi's normalizePath (dist/utils/paths.js) turns these into plain spaces before resolving.
PI_SPACES = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
# Codex apply_patch sends the raw patch as tool_input.command (codex-rs core
# tools/handlers/apply_patch.rs:370-374, rust-v0.172.0); these headers name its files.
PATCH_FILE = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+)")
PATCH_MOVE = re.compile(r"\*\*\* Move to: (.+)")


def patch_paths(patch):
    """(written, deleted, added, moved) paths of one apply_patch body; ValueError when
    unreadable. An Add File or a Move target replaces whatever file is already there."""
    if not isinstance(patch, str) or not patch.lstrip().startswith("*** Begin Patch"):
        raise ValueError("not an apply_patch body")
    written, deleted, added, moved, source = [], [], [], [], None
    for line in patch.splitlines():
        if match := PATCH_FILE.fullmatch(line.rstrip()):
            kind, path = match[1], match[2].strip()
            source = path if kind == "Update" else None
            (deleted if kind == "Delete" else written).append(path)
            if kind == "Add":
                added.append(path)
        elif (match := PATCH_MOVE.fullmatch(line.rstrip())) and source:
            written.append(match[1].strip())
            moved.append(match[1].strip())
            deleted.append(source)
    if not written and not deleted:
        raise ValueError("apply_patch names no file")
    return written, deleted, added, moved


def pi_path(value):
    """The path pi's tools resolve (dist/core/tools/path-utils.js resolveToCwd): Unicode
    spaces become spaces, a leading @ is dropped and ~ expands. None for a file URL pi would
    read on another host, which nothing here can judge."""
    value = PI_SPACES.sub(" ", value).removeprefix("@")
    if value == "~" or value.startswith("~/"):
        return str(Path.home() / value[2:])
    if value.startswith("file://"):
        url = urllib.parse.urlparse(value)
        return urllib.request.url2pathname(url.path) if url.netloc in ("", "localhost") else None
    return value


def native_call(provider, tool, inputs):
    """One native tool request in Claude's terms: (tool, paths, command, argv, deletes,
    creates, replaces). Codex names its shell Bash with a string or argv-list command and
    its edits apply_patch (codex-rs core tools/hook_names.rs); pi uses lowercase names."""
    if provider == "pi":
        tool = PI_TOOLS.get(tool, tool)
    command = inputs.get("command")
    if provider == "codex" and tool == "apply_patch":
        written, deleted, added, moved = patch_paths(command)
        return "Edit", written, None, None, deleted, added, (*added, *moved)
    argv = None
    if provider == "codex" and isinstance(command, list):
        if not command or not all(isinstance(word, str) and word for word in command):
            raise ValueError("unreadable argv")
        argv, command = tuple(command), None
    if command is not None and not isinstance(command, str):
        raise ValueError("unreadable command")
    paths = [inputs[key] for key in ("file_path", "notebook_path", "path") if key in inputs]
    if provider == "pi":
        paths = [pi_path(path) if isinstance(path, str) else path for path in paths]
    whole = paths if tool == "Write" else []
    return tool, paths, command, argv, (), whole, whole


def scratch_roots():
    """Canonical temp roots crew may write with the edit grant."""
    return tuple(sorted({str(Path(tempfile.gettempdir()).resolve()), str(Path("/tmp").resolve())}))


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


def permission_decision(events_path, event, reuse=None):
    """Bind one native tool request to this incarnation's active assignment grants.

    `reuse` is the approval this exact call earned at PreToolUse; a captain-grant spent
    there is honoured only for the same assignment while it and the lifetime still stand.
    """
    try:
        meta = read_json(events_path.parent.parent / "session.json")
        if refusal := plugin_denial(events_path, event, meta):
            return refusal
        directory, _, assignment = crew_for_events(events_path)
        # Local, like this module's other upper imports: each hook is a fresh process.
        from .execution import root
        from .protocol import protected_paths

        _, record, current = _incarnation_crew(directory, assignment["incarnation_id"], meta)
        if lifetime.revoked(current, record):
            return Decision("action-not-granted")
        project = str(root(current, record))
        provider = event.get("provider", "claude")
        # pi's extension forwards ctx.cwd; pi's tools fall back to the execution root too.
        cwd = event.get("cwd") or (project if provider == "pi" else None)
        tool, inputs = event["tool_name"], event["tool_input"]
        if not all(isinstance(value, str) and value for value in (project, cwd, tool)):
            return Decision("unparsed")
        if not isinstance(inputs, dict) or not Path(cwd).is_absolute():
            return Decision("unparsed")
        call = native_call(provider, tool, inputs)
        tool, paths, command, argv, deletes, creates, replaces = call
        if None in paths:
            return Decision("flagged-boundary")  # a path spelling the provider resolves elsewhere
        if any(not isinstance(path, str) or not path for path in (*paths, *deletes)):
            return Decision("unparsed")
        if not assignment or assignment["state"] == "done":
            return Decision("action-not-granted")
        bound = assignment["id"]
        if (reuse or {}).get("code") == "captain-grant" and reuse.get("assignment") == bound:
            return Decision("captain-grant", assignment=bound)
        absolute = lambda values: tuple(str((Path(cwd) / p).resolve()) for p in values)  # noqa: E731
        request = Request(
            tool=tool,
            project=str(Path(project).resolve()),
            cwd=str(Path(cwd).resolve()),
            owned=tuple(assignment["paths"]),
            actions=tuple(assignment["actions"]),
            command=command,
            argv=argv,
            paths=absolute(paths),
            deletes=absolute(deletes),
            replaces=absolute(replaces),
            claimed=tuple(assignment.get("claimed", ())),
            recoverable=trusted_created(assignment),
            scratch=scratch_roots(),
            fenced=tuple(str(path) for path in protected_paths()),
            grants=tuple(grant["command"] for grant in assignment.get("grants", ())),
        )
        decision = claim_decision(events_path, current, record, bound, request)
        # A grant is spent on its first exact match, even when this call needed no grant.
        matched = request.tool in SHELL_TOOLS and command_text(request) in request.grants
        if decision.code == "captain-grant" or matched:
            if not consume_grant(directory, bound, request) and decision.code == "captain-grant":
                decision = judged(replace(request, grants=()))
        if decision.verdict == Verdict.ALLOW_ONCE and request.tool in EDIT_TOOLS:
            # Pending until the call is known to have run (settle_created); the crew's own
            # files it rewrites are re-identified the same way.
            new = [p for p in absolute(creates) if not os.path.lexists(p)]
            new += [p for p in request.paths if p in request.recoverable and p not in new]
            if new:
                note_creating(directory, bound, event.get("tool_use_id"), new)
        return replace(decision, assignment=bound)
    except (CaptainError, OSError, ValueError, KeyError, StopIteration, TypeError):
        return Decision("unparsed")


def recoverable(root, path):
    """Whether HEAD holds every file under path and nothing there differs or is untracked."""
    from .protocol import _git

    tracked = _git(root, "ls-files", "-z", "--", path)
    if tracked is None or tracked.returncode or not tracked.stdout:
        return False
    status = ("status", "--porcelain", "-z", "--ignored", "--untracked-files=all")
    dirty = _git(root, *status, "--", path)
    return dirty is not None and dirty.returncode == 0 and not dirty.stdout


def judged(request):
    """decide, then prove the recoverability a flagged delete waits on."""
    decision = decide(request)
    if decision.code != "flagged-loss" or not decision.paths:
        return decision
    found = tuple(path for path in decision.paths if recoverable(request.project, path))
    if len(found) != len(decision.paths):
        return Decision(decision.code)
    return decide(replace(request, recoverable=(*request.recoverable, *found)))


def consume_grant(directory, assignment_id, request):
    """Spend one exact-command captain grant under the protocol lock; False if already spent."""
    from .protocol import checkpoint

    with checkpoint(directory) as state:
        assignment = state["assignments"].get(assignment_id)
        if not assignment or assignment["state"] == "done":
            return False
        grants = assignment.get("grants") or []
        match = next((g for g in grants if g["command"] == command_text(request)), None)
        if match is None:
            return False
        grants.remove(match)
        return True


def file_identity(path):
    """[device, inode] of a regular file, or None: a replaced file gets a new identity."""
    try:
        status = os.lstat(path)
    except OSError:
        return None
    return [status.st_dev, status.st_ino] if stat.S_ISREG(status.st_mode) else None


def trusted_created(assignment):
    """Files this assignment created that are still the very files it created."""
    created = assignment.get("created")
    if not isinstance(created, dict):
        return ()
    return tuple(path for path, identity in created.items() if file_identity(path) == identity)


def note_creating(directory, assignment_id, call, paths):
    """Remember files an allowed call is about to write as this assignment's own; they count
    as created only once settle_created sees the call ran."""
    from .protocol import checkpoint

    with checkpoint(directory) as state:
        assignment = state["assignments"].get(assignment_id)
        if assignment and assignment["state"] != "done":
            pending = assignment.setdefault("creating", [])
            for path in paths:
                if {"path": path, "call": call} not in pending:
                    pending.append({"path": path, "call": call})


def settle_created(events_path, event, provider):
    """Record pending creations once their call ran: at its PostToolUse where the provider
    has one, else at the crew's next hook event, when a sequential call has finished. A
    failed call or a missing file records nothing."""
    from .protocol import checkpoint

    kind = event.get("hook_event_name")
    if provider in POST_TOOL_PROVIDERS:
        call = event.get("tool_use_id")
        if kind not in POST_TOOL_HOOKS or not call:
            return
        ran = lambda entry: entry.get("call") == call  # noqa: E731
    else:
        ran = lambda entry: True  # noqa: E731
    directory, _, assignment = crew_for_events(events_path)
    if not assignment or not any(ran(entry) for entry in assignment.get("creating", ())):
        return
    with checkpoint(directory) as state:
        assignment = state["assignments"].get(assignment["id"])
        if not assignment or assignment["state"] == "done":
            return
        pending = assignment.get("creating", [])
        assignment["creating"] = [entry for entry in pending if not ran(entry)]
        created = assignment.get("created")
        created = created if isinstance(created, dict) else {}
        for entry in pending:
            identity = file_identity(entry["path"]) if ran(entry) else None
            if identity and kind != "PostToolUseFailure":
                created[entry["path"]] = identity
        assignment["created"] = created


def claim_decision(events_path, current, record, assignment_id, request):
    """Reserve unowned native-edit targets under the same locks as recruitment."""
    from .protocol import (
        checkpoint,
        held_by_other,
        overlaps,
        owned_paths,
        protected_paths,
    )

    decision = judged(request)
    if decision.code != "outside-grant" or request.tool not in EDIT_TOOLS:
        return decision
    project = Path(current.meta["project"])
    directory = current.directory
    with lock(state_storage(project) / "allocation.lock"), checkpoint(directory) as state:
        _, _, assignment = crew_for_events(events_path)
        if (
            not assignment
            or assignment["id"] != assignment_id
            or assignment["state"] == "done"
            or lifetime.revoked(current, record)
        ):
            return Decision("action-not-granted")
        request = replace(
            request,
            owned=tuple(assignment["paths"]),
            actions=tuple(assignment["actions"]),
            claimed=tuple(assignment.get("claimed", ())),
            recoverable=trusted_created(assignment),
        )
        decision = judged(request)
        if decision.code != "outside-grant" or request.tool not in EDIT_TOOLS:
            return decision
        root = Path(request.project)
        if not all(Path(path).is_relative_to(root) for path in request.paths):
            return Decision("flagged-boundary")
        if any(protected(Path(path).relative_to(root).parts) for path in request.paths) or any(
            overlaps(path, p) for path in request.paths for p in protected_paths()
        ):
            return Decision("flagged-boundary")
        try:
            paths = owned_paths(root, request.paths)
        except CaptainError:
            return Decision("flagged-boundary")
        # A claimed path is not assigned: replacing its uncommitted work is flagged (decide).
        proposed = replace(
            request,
            owned=tuple(sorted(set(request.owned) | set(paths))),
            claimed=tuple(sorted(set(request.claimed) | set(paths))),
        )
        allowed = judged(proposed)
        if allowed.verdict != Verdict.ALLOW_ONCE:
            return allowed if allowed.code in FLAGGED else decision
        if held_by_other(project, state, directory, assignment_id, root, paths):
            return Decision("flagged-crew")
        state["assignments"][assignment_id]["paths"] = list(proposed.owned)
        claimed = state["assignments"][assignment_id].setdefault("claimed", [])
        claimed.extend(path for path in paths if path not in claimed)
        return allowed


def pretool_decision(records, event):
    """Find an unconsumed decision for this exact native call and execution context.

    Codex's PermissionRequest carries no tool_use_id (codex-rs hooks schema.rs:302-319),
    so its call is bound by turn instead.
    """
    identity = ("session_id", "tool_use_id", "cwd", "incarnation_id")
    if not event.get("tool_use_id") and event.get("provider") == "codex":
        identity = ("session_id", "turn_id", "cwd", "incarnation_id")
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
        inputs = record.get("tool_input"), event.get("tool_input")
        if identity[1] == "turn_id" and all(isinstance(value, dict) for value in inputs):
            # Codex adds an optional description to the approval's input (core
            # tools/sandboxing.rs:142-158); the command is the call.
            inputs = tuple(value.get("command") for value in inputs)
        if record.get("tool_name") == event.get("tool_name") and inputs[0] == inputs[1]:
            return record
    return None


def permission_output(decision, hook="PermissionRequest", provider="claude"):
    """The native hook response. PreToolUse only allows or denies; PermissionRequest is
    total, so no native prompt is ever left for a user the crew cannot reach."""
    message = f"Captain approval [{decision.code}]: {decision.reason}"
    if hook == "PreToolUse":
        if decision.verdict == Verdict.DEFER:
            return None
        if decision.verdict == Verdict.ALLOW_ONCE:
            # Codex rejects a PreToolUse allow as unsupported (codex-rs hooks
            # events/pre_tool_use.rs:485); silence lets the call proceed.
            if provider == "codex":
                return None
            return {"hookSpecificOutput": {"hookEventName": hook, "permissionDecision": "allow"}}
        native = {
            "hookEventName": hook,
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
        return {"hookSpecificOutput": native}
    if decision.verdict == Verdict.ALLOW_ONCE:
        native = {"behavior": "allow"}
    else:
        if decision.verdict == Verdict.DEFER:
            message += f". No policy allows it and crew get no prompt. {FLAG}"
        native = {"behavior": "deny", "message": message}
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
        kind = event.get("hook_event_name")
        # Only a managed crew incarnation is gated; the captain keeps its native prompts.
        gated = bool(incarnation) and provider in GATED_PROVIDERS and kind in GATE_HOOKS
        if (provider != "claude" or kind in GATE_HOOKS) and not gated:
            kind = None
        if incarnation and provider in GATED_PROVIDERS:
            try:
                settle_created(Path(events_path), event, provider)
            except (CaptainError, OSError, ValueError, KeyError, TypeError):
                pass  # an unsettled creation only stays untrusted
        if kind == "PreToolUse":
            permission = permission_decision(Path(events_path), event)
            if permission.code == "unknown-tool":
                permission = None  # Keep activity evidence without changing native permissions.
        prior = pretool_decision(records, event) if kind == "PermissionRequest" else None
        if prior and prior["approval"]["code"] != "captain-grant":
            prior = None
        if kind == "PermissionRequest":
            # Grants, lifetime and filesystem paths may have changed since PreToolUse; a
            # captain grant this call already spent there is honoured for the same
            # assignment, never spent twice and never carried across a handoff.
            permission = permission_decision(
                Path(events_path), event, prior["approval"] if prior else None
            )
        if kind == "PermissionRequest" or (kind == "PreToolUse" and permission is not None):
            event["approval"] = {"verdict": permission.verdict.value, "code": permission.code}
            if prior and permission.code == "captain-grant":
                event["approval"]["pretool"] = prior["tool_use_id"]
            if kind == "PreToolUse":
                event["approval"]["source"] = kind
                if permission.assignment:
                    event["approval"]["assignment"] = permission.assignment
            # Every request resolves here: a flag or deny carries its reason to the crew, and
            # no native prompt stays open, so this is audit evidence, never pending approval.
            event["hook_event_name"] = "PermissionDecision"
            event["status"] = f"working: approval {permission.verdict.value} [{permission.code}]"
        file.write(event_line(event))
        file.flush()
    if (
        permission is not None
        and incarnation
        and crew.is_supervised
        and (
            permission.verdict == Verdict.DENY
            or (kind == "PermissionRequest" and permission.verdict != Verdict.ALLOW_ONCE)
        )
    ):
        # Supervised crew only, so a Herdr hook never takes the protocol lock here.
        from .protocol import note_denial

        try:
            note_denial(crew, event)
        except (CaptainError, OSError, ValueError, KeyError):
            pass  # a lost denial only leaves the next ask without its grant request
    if incarnation:
        try:
            native_history.observe(crew, event)
        except (CaptainError, OSError, ValueError, TypeError) as exc:
            print(
                f"captain: native history observation retained for review: {exc}", file=sys.stderr
            )
    if permission is not None:
        output = permission_output(permission, kind, provider)
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
