"""The pi RPC adapter: one supervised crew over strict JSONL records."""

import json
import os
import threading
import time

from . import config, jsonrpc
from .runtime import CaptainError

STARTING = "starting"
FINISHING = "finishing"
UNCONFIRMED = "delivery unconfirmed"
DIALOG_METHODS = frozenset(("select", "confirm", "input", "editor"))


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


def state(link):
    current = _state(link)
    return "working" if link.turn is not None or current["isStreaming"] else "idle"


def _cancel_start(link):
    with link.guard:
        if link.turn == STARTING:
            link.turn = None
        link.native.pop("sending", None)


def ring(link, message_id, text):
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
    try:
        result = link.peer.request("prompt", {"message": text}, _timeout("request_timeout"))
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


def set_model(link, model):
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise CaptainError(f"[pi-rpc-model] pi model must be a provider/model ID: {model!r}")
    working = state(link) != "idle"
    link.peer.request(
        "set_model", {"provider": provider, "modelId": model_id}, _timeout("request_timeout")
    )
    link.model = model
    return {"model": model, "applies": "next-turn" if working else "now"}


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


def _cancel_dialog(link, identifier):
    try:
        link.peer.send(
            {"type": "extension_ui_response", "id": identifier, "cancelled": True},
            _timeout("request_timeout"),
        )
    except (CaptainError, OSError):
        pass


def handle(link, message):
    event = message.get("params") if isinstance(message.get("params"), dict) else {}
    kind = message.get("method")
    if kind == "extension_ui_request":
        identifier = event.get("id")
        if event.get("method") in DIALOG_METHODS and isinstance(identifier, str):
            threading.Thread(target=_cancel_dialog, args=(link, identifier), daemon=True).start()
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
