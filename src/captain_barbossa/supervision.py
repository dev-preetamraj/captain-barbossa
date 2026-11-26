"""The CLI's one door to a session's `captain supervise` process: a token-gated unix socket."""

import fcntl
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, jsonrpc
from .runtime import CaptainError

# The supervisor hands the token only to its launcher, which passes it to the captain.
TOKEN_ENV = "CAPTAIN_SUPERVISOR_TOKEN"
LOCK = "supervisor.lock"
# macOS sun_path holds 104 bytes including the terminating NUL.
SOCKET_PATH_LIMIT = 104
# The supervisor's calls on itself carry its token per thread, never in os.environ, which
# its crew inherit.
_inside = threading.local()


@contextmanager
def acting_as(token):
    _inside.token = token
    try:
        yield
    finally:
        _inside.token = None


def socket_path(directory):
    """Where this session's supervisor listens; refused in temp scratch or past sun_path."""
    root = Path(config.text("supervisor", "socket_dir")).expanduser()
    path = root / f"s-{directory.name[:12]}.sock"
    if not root.is_absolute():
        raise CaptainError(
            f"[supervisor-socket-path] [supervisor] socket_dir must be absolute: {root}"
        )
    for scratch in ("/tmp", tempfile.gettempdir()):
        if root.resolve().is_relative_to(Path(scratch).resolve()):
            raise CaptainError(
                f"[supervisor-socket-path] [supervisor] socket_dir {root} is inside {scratch}, "
                "which crew share as scratch; choose a private directory."
            )
    if len(os.fsencode(path)) >= SOCKET_PATH_LIMIT:
        raise CaptainError(
            f"[supervisor-socket-path] Socket path {path} is {SOCKET_PATH_LIMIT} bytes or more; "
            "set a shorter [supervisor] socket_dir."
        )
    return path


def alive(directory):
    """Whether a supervisor holds this session's lock; a flock probe, never a PID."""
    try:
        fd = os.open(directory / LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CaptainError(f"Cannot probe the supervisor lock in {directory}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def request(directory, method, params=None, timeout=None):
    token = os.environ.get(TOKEN_ENV) or getattr(_inside, "token", None)
    if not token:
        raise CaptainError(
            f"[supervisor-token] {TOKEN_ENV} is not set; only the supervisor's launcher holds it."
        )
    if timeout is None:
        timeout = config.number("supervisor", "request_timeout")
    deadline = time.monotonic() + timeout
    path = socket_path(directory)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.settimeout(timeout)
            connection.connect(str(path))
        except OSError as exc:
            raise CaptainError(
                f"[supervisor-gone] No supervisor answers for session {directory.name} at {path} "
                f"({exc}). Start one with `captain supervise --session {directory.name}`."
            ) from exc
        try:
            return jsonrpc.call(
                connection.fileno(), method, {**(params or {}), "token": token}, deadline
            )
        except OSError as exc:
            raise CaptainError(
                f"[supervisor-gone] The supervisor connection failed: {exc}"
            ) from exc


def _crew_request(crew, method, timeout=None, **params):
    return request(crew.session.directory, method, {"crew": crew.crew_id, **params}, timeout)


def start(crew, lifetime_ref):
    timeout = config.number("supervisor", "startup_timeout") + config.number(
        "supervisor", "request_timeout"
    )
    return _crew_request(crew, "start", timeout, lifetime_ref=lifetime_ref)


def ring(crew, message_id, text):
    return _crew_request(crew, "ring", message_id=message_id, text=text)


def interrupt(crew, reason):
    return _crew_request(crew, "interrupt", reason=reason)["outcome"]


def set_model(crew, model):
    return _crew_request(crew, "set_model", model=model)


def state(crew):
    return _crew_request(crew, "state")["state"]


def _stop_timeout():
    return config.number("supervisor", "stop_timeout") + config.number(
        "supervisor", "request_timeout"
    )


def stop(crew, reason):
    return _crew_request(crew, "stop", _stop_timeout(), reason=reason)


def shutdown(directory):
    """Stop every crew and end the supervisor; one outcome per crew."""
    return request(directory, "shutdown", timeout=_stop_timeout())["crews"]


def interrupt_all(directory, reason=None):
    """Interrupt every working supervised crew at once; one outcome per running crew."""
    timeout = 2 * config.number("supervisor", "request_timeout")
    return request(directory, "interrupt_all", {"reason": reason}, timeout)["crews"]


def resolve_grant(directory, request_id, decision, reason=None):
    params = {"request": request_id, "decision": decision, "reason": reason}
    return request(directory, "resolve_grant", params)
