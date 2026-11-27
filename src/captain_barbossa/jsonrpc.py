"""Line-delimited JSON-RPC framing: bounded lines, validated envelopes, per-request deadlines."""

import json
import os
import select
import threading
import time

from .runtime import CaptainError

# One message per line; a native app-server item can carry a whole diff.
MAX_LINE = 4 * 1024 * 1024


def _raw(data):
    return data[:2000].decode("utf-8", "replace")


def _identifier(value):
    return type(value) in (int, str)


def decode(line):
    """The message in one line, or CaptainError carrying the raw line.

    The `jsonrpc` member is optional because Codex app-server omits it.
    """
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise CaptainError(f"[jsonrpc-malformed] Not a JSON-RPC line: {_raw(line)}") from exc
    valid = isinstance(message, dict) and message.get("jsonrpc", "2.0") == "2.0"
    if valid and "method" in message:
        valid = (
            isinstance(message["method"], str)
            and ("id" not in message or _identifier(message["id"]))
            and isinstance(message.get("params", {}), dict | list)
        )
    elif valid:
        error = message.get("error")
        valid = (
            "id" in message
            and (message["id"] is None or _identifier(message["id"]))
            and ("result" in message) != ("error" in message)
            and (
                "error" not in message
                or isinstance(error, dict)
                and type(error.get("code")) is int
                and isinstance(error.get("message"), str)
            )
        )
    if not valid:
        raise CaptainError(f"[jsonrpc-malformed] Not a JSON-RPC message: {_raw(line)}")
    return message


def _wait(fd, events, deadline):
    if deadline is None:
        return
    poller = select.poll()
    poller.register(fd, events)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not poller.poll(remaining * 1000):
        raise CaptainError(
            "[jsonrpc-timeout] The JSON-RPC peer did not answer before its deadline."
        )


def write(fd, message, deadline=None):
    data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
    if len(data) > MAX_LINE:
        raise CaptainError(f"[jsonrpc-line] A JSON-RPC message exceeds {MAX_LINE} bytes.")
    while data:
        _wait(fd, select.POLLOUT, deadline)
        try:
            data = data[os.write(fd, data) :]
        except BlockingIOError:
            continue


class LineReader:
    """Messages from one descriptor; the buffer carries bytes past a line between reads."""

    def __init__(self, fd, limit=MAX_LINE):
        self.fd, self.limit, self.buffer = fd, limit, b""

    def read(self, deadline=None):
        """The next message, None at a clean end of stream."""
        while b"\n" not in self.buffer:
            if len(self.buffer) > self.limit:
                raise CaptainError(
                    f"[jsonrpc-line] A JSON-RPC line exceeds {self.limit} bytes: {_raw(self.buffer)}"
                )
            _wait(self.fd, select.POLLIN, deadline)
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                if self.buffer:
                    raise CaptainError(
                        f"[jsonrpc-truncated] The peer closed mid-line: {_raw(self.buffer)}"
                    )
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        if len(line) > self.limit:
            raise CaptainError(
                f"[jsonrpc-line] A JSON-RPC line exceeds {self.limit} bytes: {_raw(line)}"
            )
        return decode(line)


def call(fd, method, params, deadline):
    """One request on a fresh connection; anything but its own answer is a protocol error."""
    write(fd, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, deadline)
    message = LineReader(fd).read(deadline)
    if message is None:
        raise CaptainError(f"[jsonrpc-closed] The JSON-RPC peer closed before answering {method}.")
    if "method" in message or message["id"] != 1:
        raise CaptainError(f"[jsonrpc-malformed] Unexpected reply to {method}: {message}")
    if "error" in message:
        raise CaptainError(message["error"]["message"])
    return message["result"]


class Peer:
    """A reader thread routes answers by id, the rest to `handler`; one lock serializes writes."""

    def __init__(self, read_fd, write_fd, handler, name="peer"):
        self.reader, self.write_fd, self.handler = LineReader(read_fd), write_fd, handler
        # Nonblocking, so a peer that stops reading costs a writer its deadline, not forever.
        os.set_blocking(write_fd, False)
        self.lock, self.writing = threading.Lock(), threading.Lock()
        self.pending, self.next_id, self.closed = {}, 0, None
        self.thread = threading.Thread(target=self._read, name=f"jsonrpc-{name}", daemon=True)

    def start(self):
        self.thread.start()

    def _send(self, message, deadline):
        if not self.writing.acquire(timeout=max(0, deadline - time.monotonic())):
            raise CaptainError("[jsonrpc-timeout] Another write held the connection too long.")
        try:
            if self.write_fd is None:
                raise CaptainError(f"[jsonrpc-closed] {self.closed or 'The connection is closed.'}")
            write(self.write_fd, message, deadline)
        finally:
            self.writing.release()

    def request(self, method, params, timeout):
        deadline = time.monotonic() + timeout
        slot = {"event": threading.Event(), "message": None}
        with self.lock:
            if self.closed is not None:
                raise CaptainError(f"[jsonrpc-closed] {self.closed} ({method} not sent)")
            self.next_id += 1
            identifier = self.next_id
            self.pending[identifier] = slot
        try:
            message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            self._send(message, deadline)
            if not slot["event"].wait(max(0, deadline - time.monotonic())):
                raise CaptainError(f"[jsonrpc-timeout] No answer to {method} within {timeout}s.")
        finally:
            with self.lock:
                self.pending.pop(identifier, None)
        answer = slot["message"]
        if answer is None:
            raise CaptainError(f"[jsonrpc-closed] {self.closed} ({method} unanswered)")
        if "error" in answer:
            raise CaptainError(f"[jsonrpc-error] {method}: {answer['error']['message']}")
        return answer["result"]

    def notify(self, method, timeout):
        self._send({"jsonrpc": "2.0", "method": method}, time.monotonic() + timeout)

    def respond(self, identifier, result, timeout):
        message = {"jsonrpc": "2.0", "id": identifier, "result": result}
        self._send(message, time.monotonic() + timeout)

    def fail(self, identifier, code, message, timeout):
        error = {"code": code, "message": message}
        self._send({"jsonrpc": "2.0", "id": identifier, "error": error}, time.monotonic() + timeout)

    def close(self):
        """Close the write side once any write in flight reaches its own deadline."""
        with self.writing:
            if self.write_fd is not None:
                os.close(self.write_fd)
                self.write_fd = None

    def _read(self):
        reason = "The peer closed its output."
        try:
            while (message := self.reader.read()) is not None:
                if "method" in message:
                    self.handler(message)
                    continue
                with self.lock:
                    slot = self.pending.get(message["id"])
                if slot is not None:
                    slot["message"] = message
                    slot["event"].set()
        except (CaptainError, OSError) as exc:
            reason = str(exc)
        finally:
            os.close(self.reader.fd)
            with self.lock:
                self.closed = reason
                waiting, self.pending = list(self.pending.values()), {}
            for slot in waiting:
                slot["event"].set()
