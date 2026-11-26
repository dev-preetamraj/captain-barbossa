"""The Codex app-server adapter: one Codex thread per supervised crew over stdio JSON-RPC."""

import shlex
import threading

from . import __version__, config, instructions, jsonrpc
from .runtime import CaptainError

# Set while turn/start is in flight, so a second ring cannot start a turn beside it.
STARTING = "starting"
# A turn/start that timed out: neither delivered nor lost until Codex reports a turn.
UNCONFIRMED = "delivery unconfirmed"
ASK_INSTEAD = "Declined: crew never ask the user directly; use `captain ask` instead."
CHANGE_HEADERS = {"add": "Add", "delete": "Delete", "update": "Update"}
# The hooks the gate needs live: without PreToolUse, on-request runs in-sandbox calls ungated.
GATE_HOOKS = ("sessionStart", "stop", "preToolUse", "permissionRequest")
# Codex runs a hook whose trust status is one of these; untrusted and modified ones never run.
RUNNING = ("trusted", "managed")


def _timeout(name):
    return config.number("supervisor", name)


def open(link):
    """Hand the pipes to a Peer, then initialize and start this crew's one thread."""
    link.peer = jsonrpc.Peer(
        link.stdout, link.stdin, lambda message: handle(link, message), link.crew_id
    )
    link.stdin = link.stdout = None
    link.peer.start()
    timeout = _timeout("startup_timeout")
    client = {"name": "captain-barbossa", "version": __version__}
    link.peer.request("initialize", {"clientInfo": client}, timeout)
    link.peer.notify("initialized", timeout)
    if not link.captain:
        # The captain keeps its native prompts, which the supervisor puts to the user instead.
        require_live_hooks(link, link.peer.request("hooks/list", {"cwds": [link.root]}, timeout))
    params = {"cwd": link.root, "sandbox": "workspace-write", "approvalPolicy": "on-request"}
    if link.model:
        params["model"] = link.model
    started = link.peer.request("thread/start", params, timeout)
    thread = started.get("thread") if isinstance(started, dict) else None
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise CaptainError(f"[codex-app-server] thread/start returned no thread id: {started}")
    link.thread = thread["id"]


def require_live_hooks(link, listed):
    """Refuse to start unless every captain hook is declared, enabled and trusted."""
    command = shlex.join([*instructions.hook_command(link.events)[:-1], "--environment"])
    rows = listed.get("data") if isinstance(listed, dict) else None
    hooks = [
        hook
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and isinstance(row.get("hooks"), list)
        for hook in row["hooks"]
        if isinstance(hook, dict) and hook.get("command") == command
    ]
    declared = {hook.get("eventName") for hook in hooks}
    if missing := [event for event in GATE_HOOKS if event not in declared]:
        raise CaptainError(
            f"[codex-hooks-missing] Codex does not load captain's {', '.join(missing)} hooks; "
            "update Codex to 0.164.0 or newer, then recruit again."
        )
    if idle := sorted(
        {
            hook.get("eventName")
            for hook in hooks
            if hook.get("trustStatus") not in RUNNING or hook.get("enabled") is False
        }
    ):
        raise CaptainError(
            f"[codex-hooks-untrusted] Codex has not trusted captain's {', '.join(idle)} hooks, "
            "so the gate would not run. Once, recruit a Codex crew in a Herdr session and "
            "approve its hook review screen (Codex keeps that trust per hook command), then "
            "recruit again. Captain never writes Codex trust."
        )


def state(link):
    if link.native.get("approvals"):
        return "blocked"
    return "working" if link.turn is not None else "idle"


def ring(link, message_id, text):
    """Start a turn carrying text; a crew mid-turn holds it, as a busy pane holds a doorbell."""
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        link.turn = STARTING
        link.native.update(inputs=[text], reply="", sending=message_id)
    params = {"threadId": link.thread, "input": [{"type": "text", "text": text}]}
    if link.model:
        params["model"] = link.model
    if link.writable:
        params["sandboxPolicy"] = {"type": "workspaceWrite", "writableRoots": list(link.writable)}
    try:
        started = link.peer.request("turn/start", params, _timeout("request_timeout"))
    except CaptainError as exc:
        if "[jsonrpc-timeout]" in str(exc):
            # Codex may have accepted it: the slot stays taken until a turn notice settles it.
            return {"landed": False, "gate": UNCONFIRMED}
        with link.guard:
            if link.turn == STARTING:
                link.turn = None
        raise
    turn = _turn_id(started)
    with link.guard:
        link.native["accepted"].add(message_id)
        if link.turn == STARTING:
            finished = turn is None or turn in link.native.setdefault("finished", set())
            link.turn = None if finished else turn
    return {"landed": True, "gate": None}


def interrupt(link, reason):
    turn = link.turn
    if turn is None or turn == STARTING:
        return "idle"
    params = {"threadId": link.thread, "turnId": turn}
    link.peer.request("turn/interrupt", params, _timeout("request_timeout"))
    return "interrupted"


def set_model(link, model):
    link.model = model
    return {"model": model, "applies": "next-turn" if link.turn is not None else "now"}


def _turn_id(params):
    turn = params.get("turn") if isinstance(params, dict) else None
    identifier = turn.get("id") if isinstance(turn, dict) else None
    return identifier if isinstance(identifier, str) else None


def settled(link):
    """A turn notice proves the turn/start in flight was accepted; the caller holds the guard."""
    if link.turn == STARTING and link.native.get("sending"):
        link.native.setdefault("accepted", set()).add(link.native["sending"])


def handle(link, message):
    """Runs on the reader thread: requests get their own thread, so a hook never blocks it."""
    if "id" in message:
        threading.Thread(target=answer, args=(link, message), daemon=True).start()
        return
    method = message["method"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    if method == "turn/started":
        with link.guard:
            settled(link)
            turn = _turn_id(params)
            if turn and turn not in link.native.setdefault("finished", set()):
                link.turn = turn
    elif method == "item/started" and item.get("type") == "fileChange":
        link.native.setdefault("items", {})[item.get("id")] = item
    elif method == "item/completed" and item.get("type") == "agentMessage":
        link.native["reply"] = str(item.get("text") or "")
    elif method == "turn/completed":
        turn = _turn_id(params)
        with link.guard:
            settled(link)
            link.native.setdefault("finished", set()).add(turn)
            if link.turn in (turn, STARTING):
                link.turn = None
            event = {
                "type": "agent-turn-complete",
                "thread-id": link.thread,
                "turn-id": turn,
                "cwd": link.root,
                "input-messages": link.native.get("inputs", []),
                "last-assistant-message": link.native.get("reply", ""),
            }
        threading.Thread(target=link.ended, args=(event,), daemon=True).start()


def _patch(item):
    """An apply_patch body naming the files a fileChange item touches, as the gate reads it."""
    changes = item.get("changes") if isinstance(item, dict) else None
    if not isinstance(changes, list) or not changes:
        return None
    lines = ["*** Begin Patch"]
    for change in changes:
        kind = change.get("kind") if isinstance(change, dict) else None
        header = CHANGE_HEADERS.get(kind.get("type")) if isinstance(kind, dict) else None
        path = change.get("path")
        if header is None or not isinstance(path, str) or not path:
            return None
        lines.append(f"*** {header} File: {path}")
        if header == "Update" and isinstance(kind.get("move_path"), str):
            lines.append(f"*** Move to: {kind['move_path']}")
    lines.append("*** End Patch")
    return "\n".join(lines)


def decision(link, params, tool, command):
    """accept only when the gate allows; never acceptForSession, so grants stay one-shot."""
    if command is None:
        return "decline"
    event = {
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": {"command": command},
        "cwd": params.get("cwd") or link.root,
        "session_id": link.thread,
        "turn_id": params.get("turnId"),
        "tool_use_id": params.get("itemId"),
    }
    with link.guard:
        link.native["approvals"] = link.native.get("approvals", 0) + 1
    try:
        _, output = link.relay(event)
    except (CaptainError, OSError):
        return "decline"
    finally:
        with link.guard:
            link.native["approvals"] -= 1
    native = (output or {}).get("hookSpecificOutput")
    verdict = native.get("decision") if isinstance(native, dict) else None
    allowed = isinstance(verdict, dict) and verdict.get("behavior") == "allow"
    return "accept" if allowed else "decline"


def answer(link, message):
    method, identifier = message["method"], message["id"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    timeout = _timeout("request_timeout")
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        command = command if isinstance(command, str) and command else None
        result = {"decision": decision(link, params, "Bash", command)}
    elif method == "item/fileChange/requestApproval":
        item = link.native.get("items", {}).get(params.get("itemId"))
        result = {"decision": decision(link, params, "apply_patch", _patch(item))}
    elif method == "item/tool/requestUserInput" and link.captain:
        result = {"answers": link.ask("question", params) or {}}
    elif method == "item/tool/requestUserInput":
        questions = params.get("questions") if isinstance(params.get("questions"), list) else []
        result = {
            "answers": {
                question["id"]: {"answers": [ASK_INSTEAD]}
                for question in questions
                if isinstance(question, dict) and isinstance(question.get("id"), str)
            }
        }
    else:
        try:
            link.peer.fail(identifier, -32601, f"[codex-unsupported] {method}", timeout)
        except CaptainError:
            pass  # the thread is closing; Codex sees the request unanswered either way
        return
    try:
        link.peer.respond(identifier, result, timeout)
    except CaptainError:
        pass  # the thread is closing; Codex sees the request unanswered either way
