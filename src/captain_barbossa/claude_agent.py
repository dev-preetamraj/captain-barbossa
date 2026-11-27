"""The Claude adapter: one supervised crew's claude_host over stdio JSON-RPC."""

import json
import threading
from uuid import uuid4

from . import command_catalogue, config, jsonrpc, model_catalogue, processes, usage
from .pane import WAKE_LINE
from .runtime import CaptainError

AUTH = (
    "[claude-auth] claude reported an authentication failure: run `claude auth login`, "
    "or set ANTHROPIC_API_KEY for the supervisor."
)
WORKING = "working"
FINISHING = "finishing"
UNCONFIRMED = "delivery unconfirmed"
ASK_USER = "AskUserQuestion"
# claude's settings layer drops max, so a running session takes it only from --effort at start.
RUNNING_EFFORTS = tuple(level for level in model_catalogue.CLAUDE_EFFORTS if level != "max")


def _timeout(name):
    return config.number("supervisor", name)


def open(link):
    """Hand the pipes to a Peer, then have the host start claude."""
    link.peer = jsonrpc.Peer(
        link.stdout, link.stdin, lambda message: handle(link, message), link.crew_id
    )
    link.stdin = link.stdout = None
    link.peer.start()
    # Only the captain's host asks the user; a crew's own hooks are its whole gate.
    params = {"ask": True} if link.captain else {}
    if (effort := getattr(link, "effort", None)) is not None:
        params["effort"] = _effort(effort, model_catalogue.CLAUDE_EFFORTS)
    if (mode := getattr(link, "permission_mode", None)) is not None:
        params["permission_mode"] = mode
    try:
        started = link.peer.request("connect", params, _timeout("startup_timeout"))
    except CaptainError as exc:
        # The host's own refusal, [claude-sdk-missing] say, keeps its code.
        raise CaptainError(str(exc).removeprefix("[jsonrpc-error] connect: ")) from exc
    started = started if isinstance(started, dict) else {}
    link.thread = started.get("session_id")
    link.native["effort"] = effort
    link.native["commands"] = started.get("commands") or []


def _effort(effort, levels):
    if effort not in levels:
        raise CaptainError(
            f"[effort-unsupported] Claude takes effort {', '.join(levels)} here, not {effort!r}."
        )
    return effort


def state(link):
    return "working" if link.turn is not None else "idle"


def ring(link, message_id, text, attached=()):
    """Start a turn: a crew gets the bare wake line and its hook hands over the body; the
    captain, which has no mail, gets the text itself and its attachments."""
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        link.turn = WORKING
        link.native["sending"] = message_id
    try:
        params = {"text": WAKE_LINE}
        if link.captain:
            params = {"text": text, "attachments": list(attached)}
        link.peer.request("query", params, _timeout("request_timeout"))
    except CaptainError as exc:
        if "[jsonrpc-timeout]" in str(exc):
            # The host may have accepted it: the slot stays taken until a turn notice settles it.
            return {"landed": False, "gate": UNCONFIRMED}
        with link.guard:
            link.native.pop("sending", None)
            link.turn = None
        raise
    with link.guard:
        link.native["accepted"].add(message_id)
        link.native.pop("sending", None)
    return {"landed": True, "gate": None}


def interrupt(link, reason):
    if link.turn is None:
        return "idle"
    result = link.peer.request("interrupt", {}, _timeout("request_timeout"))
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if outcome not in ("interrupted", "idle"):
        raise CaptainError(f"[claude-host] interrupt returned {result!r}.")
    return outcome


def set_model(link, model, effort=None):
    """Claude's own set_model, then its flag-settings effort; a None keeps that setting."""
    params = {} if model is None else {"model": model}
    if effort is not None:
        params["effort"] = _effort(effort, RUNNING_EFFORTS)
    if params:
        link.peer.request("set_model", params, _timeout("request_timeout"))
    if model is not None:
        link.model = model
        link.native.pop("model", None)
    if effort is not None:
        link.native["effort"] = effort
    applies = "next-turn" if link.turn is not None else "now"
    return {**current(link), "applies": applies}


def set_permission_mode(link, mode, native=None):
    """Claude's own set_permission_mode, applied at once, mid-turn included."""
    link.peer.request("set_permission_mode", {"mode": mode}, _timeout("request_timeout"))
    link.permission_mode = link.native["permission_mode"] = mode
    return {"permission_mode": mode, "applies": "now"}


def permission_mode(link):
    """The mode claude last reported or was set to; None runs the user's own settings."""
    return link.native.get("permission_mode") or getattr(link, "permission_mode", None)


def current(link):
    """The model claude last reported, else the one asked for; effort is None at claude's default."""
    return {"model": link.native.get("model") or link.model, "effort": link.native.get("effort")}


def commands(link):
    """Claude's initialize-reply commands, its skills told apart by their SKILL.md files."""
    return command_catalogue.claude_listing(link.native.get("commands") or [], link.root)


def run_command(link, name, args):
    """Claude runs a command or skill typed as the prompt `/name args`."""
    text = f"/{name} {args}".rstrip()
    return ring(link, f"command-{uuid4().hex}", text)


def idle_event(link):
    """Claude's own idle notification shape, so status reads an interrupted turn as ended."""
    return {
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "message": "The turn ended without a Stop hook (interrupted or failed).",
        "session_id": link.thread,
        "cwd": link.root,
    }


def settled(link):
    """A turn notice proves the query in flight was accepted; the caller holds the guard."""
    if sending := link.native.pop("sending", None):
        link.native.setdefault("accepted", set()).add(sending)


def finish(link, event):
    """Record an unclean idle before the slot reopens, so no new turn's hook lands ahead of it."""
    try:
        link.relay(event)
    except (CaptainError, OSError):
        pass  # best effort, as for a clean turn: a missed record costs one idle
    finally:
        with link.guard:
            if link.turn == FINISHING:
                link.turn = None
    link.ended(None)


def permission(link, message):
    """Answer the captain host's permission request with the user's decision; a crew is denied."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    tool = params.get("tool")
    inputs = params.get("input") if isinstance(params.get("input"), dict) else {}
    result = {"behavior": "deny", "message": "Declined: nobody answered in the app."}
    try:
        if link.captain and tool == ASK_USER:
            result = questions(link, inputs) or result
        elif link.captain:
            rules = params.get("rules") if isinstance(params.get("rules"), list) else []
            request = {"hook_event_name": "PermissionRequest", "tool_name": tool, "cwd": link.root}
            _, output = link.relay({**request, "tool_input": inputs, "permission_rules": rules})
            native = (output or {}).get("hookSpecificOutput")
            verdict = native.get("decision") if isinstance(native, dict) else None
            if isinstance(verdict, dict) and verdict.get("behavior") == "allow":
                result = {"behavior": "allow"}
                if verdict.get("remember") is True and rules:
                    result["remember"] = True
            else:
                given = verdict.get("message") if isinstance(verdict, dict) else None
                given = given if isinstance(given, str) and given.strip() else None
                result = {
                    "behavior": "deny",
                    "message": given or "The user denied this in the app.",
                }
    except (CaptainError, OSError):
        pass  # an unanswerable request is denied
    try:
        link.peer.respond(message["id"], result, _timeout("request_timeout"))
    except CaptainError:
        pass  # the host is closing; claude sees the request unanswered either way


def questions(link, inputs):
    """AskUserQuestion put to the user; claude reads the answers keyed by question text."""
    asked = [
        question
        for question in inputs.get("questions") or ()
        if isinstance(question, dict) and isinstance(question.get("question"), str)
    ]
    payload = {
        "questions": [
            {
                "id": question["question"],
                "header": question.get("header"),
                "question": question["question"],
                "options": question.get("options") or [],
                "multiSelect": bool(question.get("multiSelect")),
            }
            for question in asked
        ]
    }
    answered = link.ask("question", payload) if asked else None
    answers = {
        key: value["answers"][0]
        for key, value in (answered or {}).items()
        if isinstance(value, dict) and value.get("answers")
    }
    return {"behavior": "allow", "answers": answers} if answers else None


def account(binary, root, timeout):
    """Status from the user's own `claude auth status`; Captain never reads Claude credentials."""
    result = processes.capture(root, [binary, "auth", "status", "--json"], timeout)
    try:
        status = json.loads(result.stdout)
    except ValueError:
        status = None
    if not isinstance(status, dict) or type(status.get("loggedIn")) is not bool:
        detail = (result.stderr or result.stdout).strip()[:500]
        return {"signed_in": None, "account": None, "detail": detail or "No status."}
    found = None
    if status["loggedIn"]:
        found = {"type": status.get("authMethod"), "email": status.get("email")}
    return {"signed_in": status["loggedIn"], "account": found, "detail": None}


def handle(link, message):
    """Runs on the reader thread; the one request a host sends is a captain's `permission`."""
    if "id" in message:
        if message.get("method") == "permission":
            threading.Thread(target=permission, args=(link, message), daemon=True).start()
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message["method"] == "result" and getattr(link, "usage", None) is not None:
        totals = link.native.setdefault("usage_totals", {})
        model = params.get("model") if isinstance(params.get("model"), str) else link.model
        link.usage(usage.claude_live(totals, params, model))
    elif message["method"] == "turn" and params.get("state") == "working":
        with link.guard:
            settled(link)
            link.turn = WORKING
    elif message["method"] == "turn" and params.get("state") == "idle":
        clean = bool(params.get("clean"))
        with link.guard:
            settled(link)
            link.turn = None if clean else FINISHING
        target, args = (link.ended, (None,)) if clean else (finish, (link, idle_event(link)))
        threading.Thread(target=target, args=args, daemon=True).start()
    elif message["method"] == "message" and params.get("kind") == "SystemMessage":
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        init = data.get("data") if isinstance(data.get("data"), dict) else {}
        if data.get("subtype") == "init":
            link.native["init"] = init
            if isinstance(init.get("model"), str):
                link.native["model"] = init["model"]
        mode = init.get("permissionMode")
        if data.get("subtype") in ("init", "status") and isinstance(mode, str) and mode:
            changed = link.native.get("permission_mode") != mode
            link.native["permission_mode"] = mode
            if changed and getattr(link, "mode_changed", None) is not None:
                threading.Thread(target=link.mode_changed, daemon=True).start()
    elif message["method"] == "attention" and params.get("code") == "claude-auth":
        threading.Thread(target=link.notice, args=("claude_auth", AUTH), daemon=True).start()
