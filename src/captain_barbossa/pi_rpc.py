"""The pi RPC adapter: one supervised crew over strict JSONL records."""

import json
import os
import threading
import time

from . import attachments, config, jsonrpc, models, usage
from .runtime import CaptainError

STARTING = "starting"
FINISHING = "finishing"
UNCONFIRMED = "delivery unconfirmed"
DIALOG_METHODS = frozenset(("select", "confirm", "input", "editor"))
# pi echoes a prompt's images in its event lines, which the reader caps at jsonrpc.MAX_LINE.
IMAGE_BUDGET = jsonrpc.MAX_LINE // 2


def _timeout(name):
    return config.number("supervisor", name)


class Peer:
    """Correlate pi command responses while streaming session events to one handler."""

    def __init__(self, read_fd, write_fd, handler, name="pi"):
        self.read_fd, self.write_fd, self.handler = read_fd, write_fd, handler
        os.set_blocking(write_fd, False)
        self.lock, self.writing = threading.Lock(), threading.Lock()
        self.pending, self.next_id, self.closed = {}, 0, None
        self.thread = threading.Thread(target=self._read, name=f"pi-rpc-{name}", daemon=True)

    def start(self):
        self.thread.start()

    def request(self, command, params, timeout):
        deadline = time.monotonic() + timeout
        slot = {"event": threading.Event(), "message": None, "command": command}
        with self.lock:
            if self.closed is not None:
                raise CaptainError(f"[pi-rpc-closed] {self.closed} ({command} not sent)")
            self.next_id += 1
            identifier = f"captain-{self.next_id}"
            self.pending[identifier] = slot
        try:
            message = {"id": identifier, "type": command, **params}
            self._send(message, deadline)
            if not slot["event"].wait(max(0, deadline - time.monotonic())):
                raise CaptainError(f"[pi-rpc-timeout] No answer to {command} within {timeout}s.")
        finally:
            with self.lock:
                self.pending.pop(identifier, None)
        answer = slot["message"]
        if answer is None:
            raise CaptainError(f"[pi-rpc-closed] {self.closed} ({command} unanswered)")
        if not answer["success"]:
            raise CaptainError(f"[pi-rpc-error] {command}: {answer.get('error') or 'failed'}")
        return answer.get("data")

    def send(self, message, timeout):
        self._send(message, time.monotonic() + timeout)

    def close(self):
        with self.writing:
            if self.write_fd is not None:
                os.close(self.write_fd)
                self.write_fd = None

    def _send(self, message, deadline):
        if not self.writing.acquire(timeout=max(0, deadline - time.monotonic())):
            raise CaptainError("[pi-rpc-timeout] Another write held the connection too long.")
        try:
            if self.write_fd is None:
                raise CaptainError(f"[pi-rpc-closed] {self.closed or 'The connection is closed.'}")
            jsonrpc.write(self.write_fd, message, deadline)
        finally:
            self.writing.release()

    def _record(self, line):
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CaptainError(f"[pi-rpc-malformed] Not a JSON record: {line[:2000]!r}") from exc
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise CaptainError(f"[pi-rpc-malformed] Not a pi RPC record: {message!r}")
        if message["type"] != "response":
            self.handler({"method": message["type"], "params": message})
            return
        valid = (
            isinstance(message.get("id"), str)
            and isinstance(message.get("command"), str)
            and type(message.get("success")) is bool
        )
        if not valid:
            raise CaptainError(f"[pi-rpc-malformed] Invalid pi response: {message!r}")
        with self.lock:
            slot = self.pending.get(message["id"])
        if slot is not None:
            if message["command"] != slot["command"]:
                raise CaptainError(
                    f"[pi-rpc-malformed] {message['id']} answered {message['command']}, "
                    f"not {slot['command']}."
                )
            slot["message"] = message
            slot["event"].set()

    def _read(self):
        reason, buffer = "pi closed its output.", b""
        try:
            while True:
                chunk = os.read(self.read_fd, 65536)
                if not chunk:
                    if buffer:
                        raise CaptainError(
                            f"[pi-rpc-truncated] pi closed mid-line: {buffer[:2000]!r}"
                        )
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > jsonrpc.MAX_LINE:
                        raise CaptainError(
                            f"[pi-rpc-line] A pi RPC line exceeds {jsonrpc.MAX_LINE} bytes."
                        )
                    self._record(line.removesuffix(b"\r"))
                if len(buffer) > jsonrpc.MAX_LINE:
                    raise CaptainError(
                        f"[pi-rpc-line] A pi RPC line exceeds {jsonrpc.MAX_LINE} bytes."
                    )
        except (CaptainError, OSError) as exc:
            reason = str(exc)
        finally:
            os.close(self.read_fd)
            with self.lock:
                self.closed = reason
                waiting, self.pending = list(self.pending.values()), {}
            for slot in waiting:
                slot["event"].set()


def _state(link, timeout=None):
    data = link.peer.request("get_state", {}, timeout or _timeout("request_timeout"))
    if not isinstance(data, dict) or type(data.get("isStreaming")) is not bool:
        raise CaptainError(f"[pi-rpc] get_state returned invalid state: {data!r}")
    return data


def open(link):
    link.peer = Peer(link.stdout, link.stdin, lambda message: handle(link, message), link.crew_id)
    link.stdin = link.stdout = None
    link.peer.start()
    current = _state(link, _timeout("startup_timeout"))
    session_id = current.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise CaptainError(f"[pi-rpc] get_state returned no session id: {current!r}")
    link.thread = session_id
    if (effort := getattr(link, "effort", None)) is not None:
        _set_thinking(link, effort)


def state(link):
    current = _state(link)
    return "working" if link.turn is not None or current["isStreaming"] else "idle"


def _cancel_start(link):
    with link.guard:
        if link.turn == STARTING:
            link.turn = None
        link.native.pop("sending", None)


def ring(link, message_id, text, attached=()):
    with link.guard:
        if message_id in link.native.setdefault("accepted", set()):
            return {"landed": True, "gate": None}
        if link.turn is not None:
            return {"landed": False, "gate": "agent not idle"}
        link.turn = STARTING
        link.native.update(inputs=[text], reply="", sending=message_id)
    try:
        current = _state(link)
    except (CaptainError, OSError):
        _cancel_start(link)
        raise
    if current["isStreaming"]:
        _cancel_start(link)
        return {"landed": False, "gate": "agent not idle"}
    text, images = attachments.prompt(text, attached, IMAGE_BUDGET)
    link.native["inputs"] = [text]
    params = {"message": text}
    if images:
        params["images"] = [
            {"type": "image", "data": attachments.encoded(data), "mimeType": record["mime"]}
            for record, data in images
        ]
    try:
        result = link.peer.request("prompt", params, _timeout("request_timeout"))
    except (CaptainError, OSError) as exc:
        if "[pi-rpc-timeout]" in str(exc):
            return {"landed": False, "gate": UNCONFIRMED}
        _cancel_start(link)
        raise
    disposition = result.get("disposition") if isinstance(result, dict) else None
    if disposition not in ("started", "queued", "handled"):
        _cancel_start(link)
        raise CaptainError(f"[pi-rpc] prompt returned invalid disposition: {result!r}")
    with link.guard:
        link.native["accepted"].add(message_id)
        link.native.pop("sending", None)
        if disposition == "handled" and link.turn == STARTING:
            link.turn = FINISHING
    if disposition == "handled":
        threading.Thread(target=_finish, args=(link, _turn_event(link)), daemon=True).start()
    return {"landed": True, "gate": None}


def interrupt(link, reason):
    turn = link.turn
    if turn in (STARTING, FINISHING):
        return "idle"
    if turn is None and not _state(link)["isStreaming"]:
        return "idle"
    link.peer.request("abort", {}, _timeout("request_timeout"))
    return "interrupted"


def close(link):
    """Best-effort abort an active run before the supervisor closes pi's stdin."""
    if link.turn in (None, STARTING, FINISHING):
        return
    try:
        link.peer.request("abort", {}, min(_timeout("request_timeout"), 1.0))
    except (CaptainError, OSError):
        pass


def _model_params(model):
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise CaptainError(f"[pi-rpc-model] pi model must be a provider/model ID: {model!r}")
    return {"provider": provider, "modelId": model_id}


def _set_thinking(link, effort):
    """pi's thinking level, checked against the levels it lists for the current model."""
    timeout = _timeout("request_timeout")
    listed = link.peer.request("get_available_thinking_levels", {}, timeout)
    levels = listed.get("levels") if isinstance(listed, dict) else None
    levels = (
        [level for level in levels if isinstance(level, str)] if isinstance(levels, list) else []
    )
    if effort not in levels:
        offered = ", ".join(levels) or "no thinking level"
        raise CaptainError(f"[effort-unsupported] pi's model takes {offered}, not {effort!r}.")
    link.peer.request("set_thinking_level", {"level": effort}, timeout)


def set_model(link, model, effort=None):
    """pi's set_model, then set_thinking_level; a refused level restores the previous model."""
    params = None if model is None else _model_params(model)
    working = state(link) != "idle"
    timeout = _timeout("request_timeout")
    previous = current(link)["model"] if params and effort is not None else None
    if params:
        link.peer.request("set_model", params, timeout)
    if effort is not None:
        try:
            _set_thinking(link, effort)
        except CaptainError:
            if previous:
                link.peer.request("set_model", _model_params(previous), timeout)
            raise
    if model is not None:
        link.model = model
    return {**current(link), "applies": "next-turn" if working else "now"}


def current(link):
    """The model and thinking level pi's get_state reports."""
    native = _state(link)
    model = native.get("model") if isinstance(native.get("model"), dict) else {}
    provider, model_id = model.get("provider"), model.get("id")
    named = f"{provider}/{model_id}" if provider and model_id else link.model
    level = native.get("thinkingLevel")
    return {"model": named, "effort": level if isinstance(level, str) else None}


def commands(link):
    """pi's get_commands split into skills and the rest; pi lists no TUI built-in there."""
    listed = link.peer.request("get_commands", {}, _timeout("request_timeout"))
    rows = listed.get("commands") if isinstance(listed, dict) else None
    found = {"commands": [], "skills": []}
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            kind = "skills" if row.get("source") == "skill" else "commands"
            found[kind].append({"name": row["name"], "description": row.get("description")})
    return found


def _settled(link):
    if sending := link.native.pop("sending", None):
        link.native.setdefault("accepted", set()).add(sending)


def _text(message):
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _turn_event(link):
    return {
        "type": "agent-turn-complete",
        "thread-id": link.thread,
        "turn-id": link.native.get("run"),
        "cwd": link.root,
        "input-messages": link.native.get("inputs", []),
        "last-assistant-message": link.native.get("reply", ""),
    }


def _finish(link, event):
    try:
        link.relay(event)
    except (CaptainError, OSError):
        pass
    finally:
        with link.guard:
            if link.turn == FINISHING:
                link.turn = None
    link.ended(None)
    record_stats(link)


def record_stats(link):
    """Hand pi's get_session_stats to the supervisor as a live usage frame, best effort."""
    if getattr(link, "usage", None) is None:
        return
    try:
        stats = link.peer.request("get_session_stats", {}, _timeout("request_timeout"))
    except (CaptainError, OSError):
        return  # unknown stays unknown until the next turn's reading
    if (frame := usage.pi_live(stats, link.model)) is not None:
        link.usage(frame)


def _respond_dialog(link, identifier, reply):
    try:
        link.peer.send(
            {"type": "extension_ui_response", "id": identifier, **reply},
            _timeout("request_timeout"),
        )
    except (CaptainError, OSError):
        pass


def _ask_dialog(link, event):
    """Put a captain's extension dialog to the user; unanswered, it is cancelled."""
    method, title = event["method"], event.get("title")
    message = event.get("message")
    reply = {"cancelled": True}
    try:
        if method == "confirm":
            text = " - ".join(part for part in (title, message) if isinstance(part, str) and part)
            payload = {"tool": "confirm", "command": text, "cwd": link.root}
            decision = link.ask("approval", payload)
            if decision is not None:
                reply = {"confirmed": decision == "approve"}
        else:
            options = [option for option in event.get("options") or () if isinstance(option, str)]
            question = {
                "id": "value",
                "header": title,
                "question": message or title,
                "options": [{"label": option} for option in options],
                "prefill": event.get("prefill") or event.get("placeholder"),
            }
            answered = link.ask("question", {"method": method, "questions": [question]}) or {}
            value = next(iter((answered.get("value") or {}).get("answers") or ()), None)
            if isinstance(value, str) and (method != "select" or value in options):
                reply = {"value": value}
    except (CaptainError, OSError):
        pass  # an unanswerable dialog is cancelled, as for a crew
    _respond_dialog(link, event["id"], reply)


def account(binary, root, timeout):
    """pi is signed in when it lists at least one authenticated model."""
    models.pi_models.cache_clear()
    try:
        served = models.pi_models()
    except CaptainError as exc:
        return {"signed_in": False, "account": None, "detail": str(exc)}
    return {"signed_in": True, "account": {"models": len(served)}, "detail": None}


def handle(link, message):
    event = message.get("params") if isinstance(message.get("params"), dict) else {}
    kind = message.get("method")
    if kind == "extension_ui_request":
        identifier = event.get("id")
        if event.get("method") in DIALOG_METHODS and isinstance(identifier, str):
            if getattr(link, "captain", False):
                target, args = _ask_dialog, (link, event)
            else:
                target, args = _respond_dialog, (link, identifier, {"cancelled": True})
            threading.Thread(target=target, args=args, daemon=True).start()
    elif kind == "agent_start":
        with link.guard:
            _settled(link)
            link.native["runs"] = link.native.get("runs", 0) + 1
            link.native["run"] = f"run-{link.native['runs']}"
            link.native["reply"] = ""
            link.turn = link.native["run"]
    elif kind == "message_end":
        reply = _text(event.get("message"))
        if reply:
            link.native["reply"] = reply
    elif kind == "agent_settled":
        with link.guard:
            _settled(link)
            if link.turn in (None, FINISHING):
                return
            link.turn = FINISHING
            turn = _turn_event(link)
        threading.Thread(target=_finish, args=(link, turn), daemon=True).start()
