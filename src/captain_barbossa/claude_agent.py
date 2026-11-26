"""The Claude adapter: one supervised crew's claude_host over stdio JSON-RPC."""

import threading

from . import config, jsonrpc
from .pane import WAKE_LINE
from .runtime import CaptainError

AUTH = (
    "[claude-auth] claude reported an authentication failure: run `claude auth login`, "
    "or set ANTHROPIC_API_KEY for the supervisor."
)
WORKING = "working"
FINISHING = "finishing"
UNCONFIRMED = "delivery unconfirmed"


def _timeout(name):
    return config.number("supervisor", name)


def open(link):
    """Hand the pipes to a Peer, then have the host start claude."""
    link.peer = jsonrpc.Peer(
        link.stdout, link.stdin, lambda message: handle(link, message), link.crew_id
    )
    link.stdin = link.stdout = None
    link.peer.start()
    try:
        started = link.peer.request("connect", {}, _timeout("startup_timeout"))
    except CaptainError as exc:
        # The host's own refusal, [claude-sdk-missing] say, keeps its code.
        raise CaptainError(str(exc).removeprefix("[jsonrpc-error] connect: ")) from exc
    link.thread = started.get("session_id") if isinstance(started, dict) else None


def state(link):
    return "working" if link.turn is not None else "idle"


def ring(link, message_id, text):
    """Start a turn: a crew gets the bare wake line and its hook hands over the body; the
    captain, which has no mail, gets the text itself."""
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        link.turn = WORKING
        link.native["sending"] = message_id
    try:
        text = text if link.captain else WAKE_LINE
        link.peer.request("query", {"text": text}, _timeout("request_timeout"))
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


def set_model(link, model):
    link.peer.request("set_model", {"model": model}, _timeout("request_timeout"))
    link.model = model
    return {"model": model, "applies": "next-turn" if link.turn is not None else "now"}


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


def handle(link, message):
    """Runs on the reader thread; the host sends only turn and attention notifications."""
    if "id" in message:
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message["method"] == "turn" and params.get("state") == "working":
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
    elif message["method"] == "attention" and params.get("code") == "claude-auth":
        threading.Thread(target=link.notice, args=("claude_auth", AUTH), daemon=True).start()
