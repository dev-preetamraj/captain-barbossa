"""The Grok ACP adapter: one supervised crew over `grok agent stdio`."""

import threading
import time

from . import __version__, config, jsonrpc, models, store
from .runtime import CaptainError

FINISHING = "finishing"
UNCONFIRMED = "delivery unconfirmed"
TOOL_KINDS = {
    "read": "Read",
    "edit": "Edit",
    "delete": "Edit",
    "execute": "Bash",
    "search": "Grep",
}


def _timeout(name):
    return config.number("supervisor", name)


def open(link):
    """Initialize ACP, verify the Grok build, and create this crew's session."""
    link.peer = jsonrpc.Peer(
        link.stdout, link.stdin, lambda message: handle(link, message), link.crew_id
    )
    link.stdin = link.stdout = None
    link.peer.start()
    timeout = _timeout("startup_timeout")
    initialized = link.peer.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {
                "name": "captain-barbossa",
                "title": "Captain Barbossa",
                "version": __version__,
            },
        },
        timeout,
    )
    info = initialized.get("agentInfo") if isinstance(initialized, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not models.grok_acp_supported(version):
        floor = ".".join(map(str, models.GROK_ACP_FLOOR))
        raise CaptainError(
            f"[grok-acp-version] Grok {version or 'unknown'} is below the verified ACP "
            f"floor {floor}; update Grok and recruit again."
        )
    created = link.peer.request("session/new", {"cwd": link.root, "mcpServers": []}, timeout)
    session_id = created.get("sessionId") if isinstance(created, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise CaptainError(f"[grok-acp] session/new returned no session id: {created!r}")
    link.thread = session_id
    if link.model:
        link.peer.request(
            "session/set_model",
            {"sessionId": link.thread, "modelId": link.model},
            timeout,
        )


def state(link):
    if link.native.get("approvals"):
        return "blocked"
    return "working" if link.turn is not None else "idle"


def ring(link, message_id, text):
    """Start one ACP prompt; its unanswered request keeps the delivery slot occupied."""
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        number = link.native.get("turns", 0) + 1
        turn = f"turn-{number}"
        done = threading.Event()
        link.native.update(
            turns=number,
            inputs=[text],
            reply="",
            sending=message_id,
            turn_done=done,
        )
        link.turn = turn
        link.native["accepted"].add(message_id)
    threading.Thread(
        target=_prompt,
        args=(link, turn, text, done),
        name=f"grok-prompt-{link.crew_id}",
        daemon=True,
    ).start()
    return {"landed": True, "gate": None}


def _prompt(link, turn, text, done):
    params = {
        "sessionId": link.thread,
        "prompt": [{"type": "text", "text": text}],
    }
    try:
        result = link.peer.request("session/prompt", params, _timeout("request_timeout"))
    except (CaptainError, OSError) as exc:
        if "timeout" in str(exc):
            link.notice("grok_delivery_unconfirmed", f"[grok-acp] {UNCONFIRMED}: {exc}")
            return
        with link.guard:
            if link.turn == turn:
                link.turn = None
        link.notice("grok_acp", f"[grok-acp] session/prompt failed: {exc}")
        done.set()
        return
    record_usage(link, result)
    with link.guard:
        if link.turn != turn:
            return
        link.turn = FINISHING
        link.native.pop("sending", None)
        event = {
            "type": "agent-turn-complete",
            "thread-id": link.thread,
            "turn-id": turn,
            "cwd": link.root,
            "input-messages": link.native.get("inputs", []),
            "last-assistant-message": link.native.get("reply", ""),
        }
    try:
        link.relay(event)
    except (CaptainError, OSError):
        pass
    finally:
        with link.guard:
            if link.turn == FINISHING:
                link.turn = None
    done.set()
    link.ended(None)


def _notify(link, method, params, timeout=None):
    deadline = time.monotonic() + (timeout or _timeout("request_timeout"))
    link.peer._send({"jsonrpc": "2.0", "method": method, "params": params}, deadline)


def interrupt(link, reason):
    if link.turn in (None, FINISHING):
        return "idle"
    _notify(link, "session/cancel", {"sessionId": link.thread})
    return "interrupted"


def close(link):
    """Best-effort cancel an active prompt before the supervisor closes ACP stdin."""
    if link.turn in (None, FINISHING):
        return
    try:
        _notify(link, "session/cancel", {"sessionId": link.thread}, 1.0)
        done = link.native.get("turn_done")
        if isinstance(done, threading.Event):
            done.wait(1.0)
    except (CaptainError, OSError):
        pass


def set_model(link, model):
    working = link.turn is not None
    link.peer.request(
        "session/set_model",
        {"sessionId": link.thread, "modelId": model},
        _timeout("request_timeout"),
    )
    link.model = model
    return {"model": model, "applies": "next-turn" if working else "now"}


def _text(content):
    if not isinstance(content, dict) or content.get("type") != "text":
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""


def handle(link, message):
    if "id" in message:
        threading.Thread(target=answer, args=(link, message), daemon=True).start()
        return
    if message["method"] != "session/update":
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    kind = update.get("sessionUpdate") or update.get("type")
    if kind == "agent_message_chunk":
        link.native["reply"] = link.native.get("reply", "") + _text(update.get("content"))
    if kind == "usage_update" or "usage" in update:
        record_usage(link, update)


def _permission_event(link, params):
    call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    inputs = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    command = inputs.get("command")
    kind = call.get("kind")
    tool = "Bash" if isinstance(command, str) else TOOL_KINDS.get(kind, str(kind or ""))
    return {
        "provider": "grok",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": inputs,
        "tool_use_id": call.get("toolCallId"),
        "session_id": params.get("sessionId") or link.thread,
        "cwd": link.root,
    }


def _permission_option(options, allowed):
    kinds = ("allow_once",) if allowed else ("reject_once", "reject_always")
    for kind in kinds:
        for option in options:
            if isinstance(option, dict) and option.get("kind") == kind:
                identifier = option.get("optionId")
                if isinstance(identifier, str):
                    return {"outcome": "selected", "optionId": identifier}
    return {"outcome": "cancelled"}


def answer(link, message):
    identifier, method = message["id"], message["method"]
    if method != "session/request_permission":
        try:
            link.peer.fail(
                identifier,
                -32601,
                f"[grok-acp-unsupported] {method}",
                _timeout("request_timeout"),
            )
        except CaptainError:
            pass
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    options = params.get("options") if isinstance(params.get("options"), list) else []
    allowed = False
    with link.guard:
        link.native["approvals"] = link.native.get("approvals", 0) + 1
    try:
        _, output = link.relay(_permission_event(link, params))
        native = (output or {}).get("hookSpecificOutput")
        decision = native.get("decision") if isinstance(native, dict) else None
        allowed = isinstance(decision, dict) and decision.get("behavior") == "allow"
    except (CaptainError, OSError):
        pass
    finally:
        with link.guard:
            link.native["approvals"] -= 1
    try:
        link.peer.respond(
            identifier,
            {"outcome": _permission_option(options, allowed)},
            _timeout("request_timeout"),
        )
    except CaptainError:
        pass


def _number(data, *names):
    for name in names:
        value = data.get(name)
        if type(value) in (int, float) and value >= 0:
            return value
    return None


def _usage(value):
    if not isinstance(value, dict):
        return None
    update = value.get("update") if isinstance(value.get("update"), dict) else value
    usage = update.get("usage") if isinstance(update.get("usage"), dict) else update
    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else usage
    input_tokens = _number(tokens, "input", "inputTokens", "input_tokens")
    output_tokens = _number(tokens, "output", "outputTokens", "output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    counts = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(
            _number(tokens, "cacheRead", "cacheReadTokens", "cache_read_tokens") or 0
        ),
        "cache_write": int(
            _number(tokens, "cacheWrite", "cacheWriteTokens", "cache_write_tokens") or 0
        ),
    }
    cost = _number(usage, "costUsd", "totalCostUsd", "total_cost_usd", "cost")
    model = usage.get("model") or update.get("model")
    context = _number(usage, "contextTokens", "context_tokens")
    return counts, cost, model, context


def record_usage(link, value):
    """Accumulate Grok's resettable session totals in a captain-owned ledger."""
    parsed = _usage(value)
    if parsed is None:
        return
    current, cost, model, context = parsed
    path = link.events.with_suffix(".grok-usage.json")
    with store.lock(path.with_suffix(".lock")):
        try:
            ledger = store.read_json(path) if path.exists() else {}
        except CaptainError:
            ledger = {}
        previous = ledger.get("current") if isinstance(ledger.get("current"), dict) else {}
        totals = ledger.get("totals") if isinstance(ledger.get("totals"), dict) else {}
        reset = any(current[key] < previous.get(key, 0) for key in current)
        for key, value in current.items():
            delta = value if reset else value - previous.get(key, 0)
            totals[key] = totals.get(key, 0) + max(delta, 0)
        previous_cost = ledger.get("current_cost")
        total_cost = totals.get("cost")
        if cost is None or (ledger and total_cost is None):
            total_cost = None
        else:
            delta = cost if previous_cost is None or cost < previous_cost else cost - previous_cost
            total_cost = (total_cost or 0) + max(delta, 0)
        totals["cost"] = total_cost
        store.write_json(
            path,
            {
                "version": 1,
                "totals": totals,
                "current": current,
                "current_cost": cost,
                "context": int(context)
                if context is not None
                else current["input"] + current["cache_read"] + current["cache_write"],
                "model": model if isinstance(model, str) and model else link.model,
            },
        )
