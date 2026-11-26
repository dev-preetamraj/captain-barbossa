"""The Grok ACP adapter: one supervised crew over `grok agent stdio`."""

import contextlib
import os
import re
import select
import shlex
import subprocess
import threading
import time

from . import __version__, config, jsonrpc, models, processes, runtime, store
from .runtime import CaptainError

FINISHING = "finishing"
DEVICE_URL = re.compile(r"https?://[^\s\"'<>]+")
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
TERMINAL_CODES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TOOL_KINDS = {
    "read": "Read",
    "execute": "Bash",
    "search": "Grep",
}
# Input keys of a partial edit; any other edit may replace the whole file, so it is a Write.
PARTIAL_EDIT = ("old_string", "old_str", "oldText", "old_text", "edits")
# Call kinds that must have asked the gate before they run.
GATED_KINDS = ("execute", "edit", "delete", "move", "fetch")
# ACP modes that ask before acting, preferred first; a session in another mode is switched.
ASKING_MODES = ("default",)


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
    version = version if isinstance(version, str) and version else cli_version(link.root)
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
    modes = created.get("modes") if isinstance(created.get("modes"), dict) else {}
    offered = {
        mode.get("id") for mode in modes.get("availableModes") or () if isinstance(mode, dict)
    }
    asking = next((mode for mode in ASKING_MODES if mode in offered), None)
    if asking and modes.get("currentModeId") not in ASKING_MODES:
        params = {"sessionId": link.thread, "modeId": asking}
        link.peer.request("session/set_mode", params, timeout)
    if link.model:
        link.peer.request(
            "session/set_model",
            {"sessionId": link.thread, "modelId": link.model},
            timeout,
        )


def cli_version(root):
    """`grok --version`, for a build whose ACP initialize names no agentInfo version."""
    try:
        result = processes.capture(root, [runtime.executable("grok"), "--version"], 10)
    except CaptainError:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if result.returncode == 0 and lines else None


def state(link):
    if link.native.get("approvals"):
        return "blocked"
    return "working" if link.turn is not None else "idle"


def ring(link, message_id, text):
    """Send one ACP prompt; it lands once sent, and its reply ends the turn whenever it comes."""
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
    try:
        identifier, slot = _send_prompt(link, text)
    except (CaptainError, OSError):
        with link.guard:
            if link.turn == turn:
                link.turn = None
            link.native.pop("sending", None)
        done.set()
        raise
    with link.guard:
        link.native["accepted"].add(message_id)
    threading.Thread(
        target=_prompt,
        args=(link, turn, identifier, slot, done),
        name=f"grok-prompt-{link.crew_id}",
        daemon=True,
    ).start()
    return {"landed": True, "gate": None}


def _send_prompt(link, text):
    """(id, reply slot) of a sent session/prompt; only the send has a deadline."""
    # Peer.request bounds the reply too, but a prompt's reply comes only when the turn ends.
    peer = link.peer
    slot = {"event": threading.Event(), "message": None}
    with peer.lock:
        if peer.closed is not None:
            raise CaptainError(f"[jsonrpc-closed] {peer.closed} (session/prompt not sent)")
        peer.next_id += 1
        identifier = peer.next_id
        peer.pending[identifier] = slot
    params = {"sessionId": link.thread, "prompt": [{"type": "text", "text": text}]}
    message = {"jsonrpc": "2.0", "id": identifier, "method": "session/prompt", "params": params}
    try:
        peer._send(message, time.monotonic() + _timeout("request_timeout"))
    except CaptainError:
        with peer.lock:
            peer.pending.pop(identifier, None)
        raise
    return identifier, slot


def _prompt(link, turn, identifier, slot, done):
    """Wait out the turn: the prompt's reply, or the connection closing, ends it."""
    slot["event"].wait()
    with link.peer.lock:
        link.peer.pending.pop(identifier, None)
    answer = slot["message"]
    if answer is None or "error" in answer:
        error = answer.get("error") if answer else None
        detail = error.get("message") if isinstance(error, dict) else link.peer.closed
        with link.guard:
            if link.turn == turn:
                link.turn = None
        stopping = getattr(link, "stopping", None)
        if not (stopping and stopping.is_set()):
            link.notice("grok_acp", f"[grok-acp] session/prompt failed: {detail}")
        done.set()
        return
    record_usage(link, answer.get("result"), per_turn=True)
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
    """Runs on the reader thread, so a permission request is noted before its call can run."""
    if "id" in message:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        if message.get("method") == "session/request_permission":
            link.native.setdefault("asked", set()).add(call.get("toolCallId"))
        threading.Thread(target=answer, args=(link, message), daemon=True).start()
        return
    if message["method"] != "session/update":
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    kind = update.get("sessionUpdate") or update.get("type")
    if kind == "agent_message_chunk":
        link.native["reply"] = link.native.get("reply", "") + _text(update.get("content"))
    if kind in ("tool_call", "tool_call_update"):
        _watch_call(link, update)
    if kind == "usage_update" or "usage" in update:
        record_usage(link, update)


def _watch_call(link, update):
    """Cancel the turn once a gated kind of call runs without having asked the gate."""
    call = update.get("toolCallId")
    kinds = link.native.setdefault("call_kinds", {})
    if not isinstance(call, str):
        return
    if isinstance(update.get("kind"), str):
        kinds[call] = update["kind"]
    if (
        update.get("status") not in ("in_progress", "completed")
        or kinds.get(call) not in GATED_KINDS
    ):
        return
    flagged = link.native.setdefault("ungated", set())
    if call in link.native.setdefault("asked", set()) or call in flagged:
        return
    flagged.add(call)
    threading.Thread(target=_ungated, args=(link, kinds[call]), daemon=True).start()


def _ungated(link, kind):
    with contextlib.suppress(CaptainError, OSError):
        interrupt(link, "ungated")
    link.notice(
        "grok_ungated",
        f"[grok-ungated] Grok ran a {kind} call without asking the gate, so Captain cancelled "
        "the turn; check Grok's allow rules and permission mode.",
    )


def _call_paths(call, inputs):
    named = [inputs.get(key) for key in ("file_path", "notebook_path", "path")]
    located = [
        place.get("path") for place in call.get("locations") or () if isinstance(place, dict)
    ]
    return list(
        dict.fromkeys(path for path in (*named, *located) if isinstance(path, str) and path)
    )


def _gate_call(call, inputs):
    """(tool, tool_input) in the gate's own terms; a call it cannot place has no path."""
    kind, paths = call.get("kind"), _call_paths(call, inputs)
    if isinstance(inputs.get("command"), str):
        return "Bash", inputs
    if kind in ("delete", "move"):
        # As shell, the gate judges what a removal loses: unowned work is flagged-loss.
        whole = paths and (kind == "delete" or len(paths) == 2)
        verb = ["rm", "--"] if kind == "delete" else ["mv"]  # the gate reads no `mv --`
        return ("Bash", {"command": shlex.join([*verb, *paths])}) if whole else ("Edit", {})
    if kind == "edit" and len(paths) == 1:
        tool = "Edit" if any(key in inputs for key in PARTIAL_EDIT) else "Write"
        return tool, {"file_path": paths[0]}
    if kind == "edit":
        return "Edit", {}
    return TOOL_KINDS.get(kind, str(kind or "")), inputs


def _permission_event(link, params):
    call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    inputs = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    tool, tool_input = _gate_call(call, inputs)
    return {
        "provider": "grok",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": tool_input,
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


def account(binary, root, timeout):
    """Grok has no status command verified here, so only XAI_API_KEY reads as signed in."""
    if os.environ.get("XAI_API_KEY"):
        return {"signed_in": True, "account": {"type": "apiKey"}, "detail": None}
    return {"signed_in": None, "account": None, "detail": "Grok reports no sign-in status."}


def sign_in(attempt, binary, root, params, deadline):
    """(state, detail) of `grok login --device-auth`, reporting each line and the device code."""
    read_fd, write_fd = os.pipe()
    env = {key: value for key, value in os.environ.items() if key not in processes.CHILD_SECRETS}
    try:
        process = subprocess.Popen(
            [binary, "login", "--device-auth"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=write_fd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(read_fd)
        raise CaptainError(f"[grok-login] grok login did not start: {exc}") from exc
    finally:
        os.close(write_fd)
    found, buffer = {}, b""
    expired = "The device code expired before sign-in finished."
    try:
        while not attempt.cancel.is_set():
            if time.monotonic() > deadline:
                return "failed", expired
            if not select.select([read_fd], [], [], 0.2)[0]:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                line = TERMINAL_CODES.sub("", raw.decode("utf-8", "replace")).strip()
                if line:
                    _device_line(attempt, line, found)
        if attempt.cancel.is_set():
            return "cancelled", None
        try:
            code = process.wait(max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "failed", expired
        return ("signed-in", None) if code == 0 else ("failed", f"grok login exited {code}.")
    finally:
        os.close(read_fd)
        if process.poll() is None:
            processes.terminate_process_group(process)


def _device_line(attempt, line, found):
    """Report a line of grok's output, and the device code once its URL is known."""
    before = dict(found)
    url = DEVICE_URL.search(line)
    code = DEVICE_CODE.search(DEVICE_URL.sub(" ", line))
    if url and "verification_url" not in found:
        found["verification_url"] = url[0]
    if code and "user_code" not in found:
        found["user_code"] = code[0]
    fields = {}
    if found != before and "verification_url" in found:
        fields = {"state": "waiting", **found}
    attempt.report(line=line, **fields)


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
            _number(tokens, "cacheRead", "cacheReadTokens", "cachedReadTokens", "cache_read_tokens")
            or 0
        ),
        "cache_write": int(
            _number(
                tokens, "cacheWrite", "cacheWriteTokens", "cachedWriteTokens", "cache_write_tokens"
            )
            or 0
        ),
    }
    cost = _number(usage, "costUsd", "totalCostUsd", "total_cost_usd", "cost")
    model = usage.get("model") or update.get("model")
    context = _number(usage, "contextTokens", "context_tokens")
    return counts, cost, model, context


def _window(value):
    """(used, size) from an ACP usage_update: tokens in context and the context window."""
    update = value.get("update") if isinstance(value, dict) else None
    update = update if isinstance(update, dict) else value if isinstance(value, dict) else {}
    used, size = update.get("used"), update.get("size")
    if type(used) is int and type(size) is int and used >= 0 and size > 0:
        return used, size
    return None


def _session_cost(value):
    """The session's cumulative USD cost from an ACP usage_update, or None."""
    update = value.get("update") if isinstance(value, dict) else None
    update = update if isinstance(update, dict) else value if isinstance(value, dict) else {}
    cost = update.get("cost") if isinstance(update.get("cost"), dict) else {}
    if cost.get("currency", "USD") != "USD":
        return None
    return _number(cost, "amount")


def record_usage(link, value, per_turn=False):
    """Keep Grok's usage in a captain-owned ledger that survives a resumed session's reset.

    Session readings are cumulative and may restart; a prompt result's usage is its turn's
    alone. The first kind seen owns the token totals, so the two are never counted twice.
    """
    parsed, window, spent = _usage(value), _window(value), _session_cost(value)
    if parsed is None and window is None and spent is None:
        return
    path = link.events.with_suffix(".grok-usage.json")
    source = "turn" if per_turn else "session"
    with store.lock(path.with_suffix(".lock")):
        try:
            ledger = store.read_json(path) if path.exists() else {}
        except CaptainError:
            ledger = {}
        if parsed is not None and ledger.get("source", source) == source:
            ledger = (_add_turn if per_turn else _accumulate)(link, ledger, *parsed)
            ledger["source"] = source
        elif spent is not None:
            ledger = _spend(ledger, spent)
        if window is not None:
            ledger.update(version=1, context=window[0], limit=window[1])
        store.write_json(path, ledger)
    if getattr(link, "usage", None) is not None:
        link.usage(None)  # the ledger is the reading; the supervisor publishes it


def _spend(ledger, amount):
    """Add a cumulative session cost reading; a lower one means the session restarted."""
    totals = dict(ledger.get("totals") or {})
    previous = ledger.get("current_cost")
    delta = amount if previous is None or amount < previous else amount - previous
    totals["cost"] = (totals.get("cost") or 0) + max(delta, 0)
    return {**ledger, "version": 1, "totals": totals, "current_cost": amount}


def _add_turn(link, ledger, current, cost, model, context):
    totals = dict(ledger.get("totals") or {})
    for key, value in current.items():
        totals[key] = totals.get(key, 0) + value
    if cost is not None:
        totals["cost"] = (totals.get("cost") or 0) + cost
    if ledger.get("limit") is None:  # no usage_update yet, so the turn's input is the context
        own = current["input"] + current["cache_read"] + current["cache_write"]
        ledger = {**ledger, "context": int(context) if context is not None else own}
    return {
        **ledger,
        "version": 1,
        "totals": totals,
        "model": model if isinstance(model, str) and model else link.model,
    }


def _accumulate(link, ledger, current, cost, model, context):
    previous = ledger.get("current") if isinstance(ledger.get("current"), dict) else {}
    totals = ledger.get("totals") if isinstance(ledger.get("totals"), dict) else {}
    reset = any(current[key] < previous.get(key, 0) for key in current)
    for key, value in current.items():
        delta = value if reset else value - previous.get(key, 0)
        totals[key] = totals.get(key, 0) + max(delta, 0)
    previous_cost = ledger.get("current_cost")
    total_cost = totals.get("cost")
    if cost is None or ("totals" in ledger and total_cost is None):
        total_cost = None
    else:
        delta = cost if previous_cost is None or cost < previous_cost else cost - previous_cost
        total_cost = (total_cost or 0) + max(delta, 0)
    totals["cost"] = total_cost
    return {
        **ledger,
        "version": 1,
        "totals": totals,
        "current": current,
        "current_cost": cost,
        "context": int(context)
        if context is not None
        else current["input"] + current["cache_read"] + current["cache_write"],
        "model": model if isinstance(model, str) and model else link.model,
    }
