"""The Codex app-server adapter: one thread per supervised agent over stdio JSON-RPC."""

import contextlib
import os
import shlex
import subprocess
import threading
import time

from . import __version__, attachments, config, instructions, jsonrpc, processes, usage
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
RESUME_REFUSED = "[jsonrpc-error] thread/resume:"
# Codex's TUI built-ins the app-server serves as methods; typed as message text they reach the model.
BUILTINS = (
    {"name": "compact", "description": "Summarize the thread to free context."},
    {"name": "review", "description": "Review the working tree's changes."},
    {"name": "model", "description": "Choose the model and reasoning effort."},
)


def _timeout(name):
    return config.number("supervisor", name)


def _recorded_thread(link):
    if not link.captain:
        return None
    if isinstance(getattr(link, "thread", None), str) and link.thread:
        return link.thread
    events = getattr(link, "events", None)
    if events is None:
        return None
    recorded = None
    for event in usage.records(events):
        thread = event.get("thread-id")
        if isinstance(thread, str) and thread:
            recorded = thread
    return recorded


def open(link):
    """Hand the pipes to a Peer, then initialize and open this agent's one thread."""
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
    params = {"cwd": link.root}
    if not link.captain:
        # A captain keeps the user's own sandbox and approval settings, as in a terminal.
        params.update(sandbox="workspace-write", approvalPolicy="on-request")
    if link.model:
        params["model"] = link.model
    method = "thread/start"
    recorded = _recorded_thread(link)
    if recorded is not None:
        method = "thread/resume"
        try:
            resume = {**params, "threadId": recorded, "excludeTurns": True}
            started = link.peer.request(method, resume, timeout)
        except CaptainError as exc:
            if not str(exc).startswith(RESUME_REFUSED):
                raise
            link.notice(
                "codex_thread_resume",
                f"[codex-thread-resume] Codex refused recorded thread {recorded}; "
                f"starting a new thread: {exc}",
            )
            method = "thread/start"
            started = link.peer.request(method, params, timeout)
    else:
        started = link.peer.request(method, params, timeout)
    thread = started.get("thread") if isinstance(started, dict) else None
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise CaptainError(f"[codex-app-server] {method} returned no thread id: {started}")
    if method == "thread/resume" and thread["id"] != recorded:
        raise CaptainError(
            f"[codex-app-server] thread/resume returned {thread['id']} for recorded thread {recorded}"
        )
    link.thread = thread["id"]
    for key, native in (("thread_model", "model"), ("thread_effort", "reasoningEffort")):
        if isinstance(started.get(native), str):
            link.native[key] = started[native]
    if (effort := getattr(link, "effort", None)) is not None:
        link.native["effort"] = _effort(link, current(link)["model"], effort)


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


def ring(link, message_id, text, attached=()):
    """Start a turn carrying text; a crew mid-turn holds it, as a busy pane holds a doorbell."""
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        link.turn = STARTING
        link.native.update(inputs=[text], reply="", sending=message_id)
    text, images = attachments.prompt(text, attached)
    link.native["inputs"] = [text]
    items = [{"type": "localImage", "path": record["path"]} for record, _ in images]
    params = {"threadId": link.thread, "input": [{"type": "text", "text": text}, *items]}
    if link.model:
        params["model"] = link.model
    if link.native.get("effort"):
        params["effort"] = link.native["effort"]
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


def set_model(link, model, effort=None):
    """Codex's per-turn model and effort; a kept effort the new model lacks falls to its default."""
    target = model or current(link)["model"]
    if effort is not None:
        effort = _effort(link, target, effort)
    elif model is not None and link.native.get("effort"):
        levels = _efforts(link, target)
        effort = link.native["effort"] if levels and link.native["effort"] in levels else None
        link.native.pop("effort", None)
    if model is not None:
        link.model = model
        link.native.pop("thread_effort", None)
    if effort is not None:
        link.native["effort"] = effort
    return {**current(link), "applies": "next-turn" if link.turn is not None else "now"}


def current(link):
    return {
        "model": link.model or link.native.get("thread_model"),
        "effort": link.native.get("effort") or link.native.get("thread_effort"),
    }


def _efforts(link, model):
    """The reasoning efforts model/list advertises for model, the source the catalogue reads."""
    cursor, seen = None, set()
    while True:
        listed = link.peer.request(
            "model/list", {"includeHidden": True, "cursor": cursor}, _timeout("request_timeout")
        )
        rows = listed.get("data") if isinstance(listed, dict) else None
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and model in (row.get("model"), row.get("id")):
                efforts = row.get("supportedReasoningEfforts")
                return [
                    effort["reasoningEffort"]
                    for effort in (efforts if isinstance(efforts, list) else ())
                    if isinstance(effort, dict) and isinstance(effort.get("reasoningEffort"), str)
                ]
        cursor = listed.get("nextCursor") if isinstance(listed, dict) else None
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return None
        seen.add(cursor)


def _effort(link, model, effort):
    levels = _efforts(link, model) if model else None
    if not levels or effort not in levels:
        offered = ", ".join(levels) if levels else "no listed effort"
        raise CaptainError(
            f"[effort-unsupported] Codex model {model or '(default)'} takes {offered}, "
            f"not {effort!r}."
        )
    return effort


def commands(link):
    """Enabled skills from skills/list for the thread's cwd, with the app-served built-ins."""
    listed = link.peer.request("skills/list", {"cwds": [link.root]}, _timeout("request_timeout"))
    rows = listed.get("data") if isinstance(listed, dict) else None
    skills = [
        {"name": skill["name"], "description": skill.get("description")}
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, dict) and isinstance(row.get("skills"), list)
        for skill in row["skills"]
        if isinstance(skill, dict) and isinstance(skill.get("name"), str) and skill.get("enabled")
    ]
    return {"commands": [dict(builtin) for builtin in BUILTINS], "skills": skills}


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
    elif method == "thread/tokenUsage/updated" and getattr(link, "usage", None) is not None:
        # On the reader thread, so readings are kept in the order Codex sent them.
        if (frame := usage.codex_live(params.get("tokenUsage"), link.model)) is not None:
            link.usage(frame)
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


def account_server(binary, root, handler, timeout):
    """(process, peer) of a bare `codex app-server` for account calls, in its own group."""
    native_in, write_fd = os.pipe()
    read_fd, native_out = os.pipe()
    env = {key: value for key, value in os.environ.items() if key not in processes.CHILD_SECRETS}
    try:
        process = subprocess.Popen(
            [binary, "app-server"],
            cwd=root,
            stdin=native_in,
            stdout=native_out,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        for fd in (write_fd, read_fd):
            os.close(fd)
        raise CaptainError(f"[codex-app-server] codex app-server did not start: {exc}") from exc
    finally:
        for fd in (native_in, native_out):
            os.close(fd)
    peer = jsonrpc.Peer(read_fd, write_fd, handler, "codex-account")
    peer.start()
    try:
        client = {"name": "captain-barbossa", "version": __version__}
        peer.request("initialize", {"clientInfo": client}, timeout)
        peer.notify("initialized", timeout)
    except CaptainError:
        close_account_server(process, peer)
        raise
    return process, peer


def close_account_server(process, peer):
    """End of input asks app-server to exit; its group is reaped either way."""
    peer.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(processes.GROUP_TERMINATE_TIMEOUT)
    processes.terminate_process_group(process)


def account(binary, root, timeout):
    """Sign-in status from Codex's own account/read; Codex keeps every credential."""
    process, peer = account_server(binary, root, lambda message: None, timeout)
    try:
        read = peer.request("account/read", {"refreshToken": False}, timeout)
    finally:
        close_account_server(process, peer)
    read = read if isinstance(read, dict) else {}
    found = read.get("account")
    if isinstance(found, dict):
        keep = {
            "type": found.get("type"),
            "email": found.get("email"),
            "plan": found.get("planType"),
        }
        return {"signed_in": True, "account": keep, "detail": None}
    if read.get("requiresOpenaiAuth") is False:
        detail = "Codex's configured provider needs no OpenAI sign-in."
        return {"signed_in": True, "account": None, "detail": detail}
    return {"signed_in": False, "account": None, "detail": None}


def sign_in(attempt, binary, root, params, deadline):
    """(state, detail) of account/login/start; an API key goes to Codex's own store only."""
    timeout = _timeout("startup_timeout")
    completed, outcome = threading.Event(), {}

    def handler(message):
        if message.get("method") == "account/login/completed" and "id" not in message:
            body = message.get("params")
            outcome.update(body if isinstance(body, dict) else {})
            completed.set()

    process, peer = account_server(binary, root, handler, timeout)
    try:
        if params["method"] == "apiKey":
            request = {"type": "apiKey", "apiKey": params["api_key"]}
            peer.request("account/login/start", request, timeout)
            read = peer.request("account/read", {"refreshToken": False}, timeout)
            found = read.get("account") if isinstance(read, dict) else None
            return ("signed-in", None) if isinstance(found, dict) else ("failed", "No account.")
        started = peer.request("account/login/start", {"type": "chatgptDeviceCode"}, timeout)
        started = started if isinstance(started, dict) else {}
        login, code, url = (started.get(key) for key in ("loginId", "userCode", "verificationUrl"))
        if not all(isinstance(value, str) and value for value in (login, code, url)):
            return "failed", f"account/login/start returned no device code: {started}"
        attempt.report(state="waiting", user_code=code, verification_url=url)
        while not completed.wait(0.2):
            if attempt.cancel.is_set() or time.monotonic() > deadline:
                with contextlib.suppress(CaptainError):
                    peer.request("account/login/cancel", {"loginId": login}, timeout)
                return ("cancelled", None) if attempt.cancel.is_set() else ("failed", "Expired.")
        if outcome.get("loginId") not in (None, login):
            return "failed", "Codex completed a different login."
        if outcome.get("success") is True:
            return "signed-in", None
        return "failed", str(outcome.get("error") or "Codex reported the sign-in failed.")
    finally:
        close_account_server(process, peer)
