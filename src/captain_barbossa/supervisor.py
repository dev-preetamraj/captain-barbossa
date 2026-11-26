"""`captain supervise`: the one process per session that owns supervised crew behind a socket."""

import hmac
import json
import os
import re
import secrets
import signal
import socketserver
import stat
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field

from . import (
    claude_agent,
    codex_app_server,
    config,
    instructions,
    jsonrpc,
    lifetime,
    processes,
    protocol,
    store,
    supervision,
)
from .crew import Crew
from .memory import add_memory
from .runtime import CaptainError
from .sessions import Session, crew_meta, session

# Provider -> adapter module (open, ring, interrupt, set_model, state over a Link).
# A provider without a row is refused at start.
ADAPTERS = {"codex": codex_app_server, "claude": claude_agent}
# Bytes of a native CLI's stderr kept per crew; the rest is read and dropped.
STDERR_LIMIT = 1024 * 1024
# How long a second supervisor waits for a lock probe to let go before refusing.
LOCK_WAIT = 1.0
# JSON-RPC error codes; error.data.code carries the captain protocol code.
INVALID_REQUEST = -32600
UNKNOWN_METHOD = -32601
REFUSED = -32000
TOKEN_ERROR = -32001
PROTOCOL_CODE = re.compile(r"\[([a-z0-9-]+)\] ")


@dataclass
class Link:
    """One supervised crew: the pipes to its native CLI and the conversation it holds."""

    crew_id: str
    ref: str
    adapter: object
    events: object
    env: dict = field(default_factory=dict)
    stdin: int | None = None
    stdout: int | None = None
    thread: str | None = None
    turn: str | None = None
    error: str | None = None
    receipt: object = None
    ready: threading.Event = field(default_factory=threading.Event)
    stopping: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    root: str | None = None
    model: str | None = None
    writable: tuple = ()
    peer: object = None
    native: dict = field(default_factory=dict)
    guard: threading.Lock = field(default_factory=threading.Lock)
    relay: object = None
    ended: object = None
    notice: object = None


@dataclass
class Supervisor:
    """`closing` refuses new crew during stop-all; `stopped` ends the serve loop."""

    session: object
    token: str
    stopped: threading.Event
    links: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closing: threading.Event = field(default_factory=threading.Event)


def refusal(message, code=REFUSED):
    """A JSON-RPC error whose message leads with, and whose data names, a protocol code."""
    match = PROTOCOL_CODE.match(message)
    protocol = match[1] if match else "supervisor-error"
    if not match:
        message = f"[{protocol}] {message}"
    return {"code": code, "message": message, "data": {"code": protocol}}


def _text(params, key):
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise CaptainError(f"[supervisor-params] {key} must be a non-empty string.")
    return value


def _link(supervisor, params):
    crew_id = _text(params, "crew")
    with supervisor.lock:
        link = supervisor.links.get(crew_id)
    if link is None or not link.ready.is_set() or link.stopping.is_set() or link.error:
        raise CaptainError(f"[supervisor-crew] {crew_id} is not running under this supervisor.")
    return link


def own(supervisor, link):
    """The owner thread: claim the lifetime, start the guard on pipes, poll like lifetime.run."""
    try:
        with lifetime.claim(supervisor.session, link.ref) as job:
            record = lifetime.read(supervisor.session, link.ref)
            link.env, link.root = record["env"], record["root"]
            native_in, link.stdin = os.pipe()
            link.stdout, native_out = os.pipe()
            errors, native_err = os.pipe()
            try:
                job.start(detached=True, stdin=native_in, stdout=native_out, stderr=native_err)
            finally:
                for fd in (native_in, native_out, native_err):
                    os.close(fd)
                keep_stderr(supervisor, link, errors)
            link.adapter.open(link)
            link.ready.set()
            poll = config.number("lifetime", "poll_interval")
            while (
                job.poll() is None
                and not link.stopping.is_set()
                and not lifetime.read(supervisor.session, link.ref)["revoked"]
            ):
                link.stopping.wait(poll)
            # End of input is a stdio server's cue to exit before the group is signalled.
            if link.peer is not None:
                link.peer.close()
            elif link.stdin is not None:
                os.close(link.stdin)
                link.stdin = None
            job.shutdown()
            link.receipt = job.finish()
    except (CaptainError, OSError) as exc:
        link.error = str(exc)
    finally:
        if link.peer is not None:
            link.peer.close()
        for fd in (link.stdin, link.stdout):
            if fd is not None:
                os.close(fd)
        link.stdin = link.stdout = None
        with supervisor.lock:
            if supervisor.links.get(link.crew_id) is link:
                del supervisor.links[link.crew_id]
        link.ready.set()
        link.done.set()


def keep_stderr(supervisor, link, fd):
    """Drain the native's stderr on a thread, keeping the first STDERR_LIMIT bytes."""
    folder = store.private_dir(supervisor.session.directory / "supervisor")
    path = folder / f"{link.events.stem}.stderr"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW

    def copy():
        with os.fdopen(fd, "rb") as source, os.fdopen(os.open(path, flags, 0o600), "wb") as out:
            kept = 0
            for chunk in iter(lambda: source.read1(65536), b""):
                room = max(STDERR_LIMIT - kept, 0)
                out.write(chunk[:room])
                out.flush()
                kept += min(len(chunk), room)

    threading.Thread(target=copy, name=f"stderr-{link.crew_id}", daemon=True).start()


def crew_of(supervisor, crew_id):
    meta = store.read_json(supervisor.session.meta_path)
    return Crew(crew_id, meta["crew"][crew_id], Session(supervisor.session.directory, meta))


def turn_finished(supervisor, link, event):
    """Record the turn (event None when a native hook already did), then ring held mail."""
    try:
        if event is not None:
            relay_hook(link, event)
        with supervision.acting_as(supervisor.token):
            protocol.turn_ended(crew_of(supervisor, link.crew_id))
    except (CaptainError, OSError, KeyError):
        pass  # best effort: the drain tick retries held mail, and a missed record costs one idle


def post_notice(supervisor, link, key, summary):
    """An attention condition the native reported, posted once per assignment."""
    try:
        protocol.once(crew_of(supervisor, link.crew_id), key, summary)
    except (CaptainError, OSError, KeyError):
        pass  # best effort: the crew's own report or a lost-crew close still reaches the captain


def drain_all(supervisor):
    with supervisor.lock:
        links = [link for link in supervisor.links.values() if link.ready.is_set()]
    for link in links:
        try:
            with supervision.acting_as(supervisor.token):
                protocol.drain(crew_of(supervisor, link.crew_id))
        except (CaptainError, OSError, KeyError):
            pass  # best effort, like the CLI's own pump_mail: the next tick retries


def bound_launch(supervisor, meta, crew_id, record, ref):
    """Refuse a lifetime that is not this crew's own recorded launch."""
    try:
        launch = lifetime.read(supervisor.session, ref)
    except (CaptainError, KeyError) as exc:
        raise CaptainError(f"[supervisor-crew] Lifetime {ref} is unreadable: {exc}") from exc
    expected = (
        crew_id,
        record.get("incarnation_id"),
        record.get("execution_root") or meta["project"],
        record.get("worktree_id"),
    )
    actual = (launch["crew_id"], launch["incarnation_id"], launch["root"], launch["worktree_id"])
    if record.get("lifetime_id") != ref or actual != expected:
        raise CaptainError(f"[supervisor-crew] Lifetime {ref} is not {crew_id}'s own launch.")


def start(supervisor, params):
    crew_id, ref = _text(params, "crew"), _text(params, "lifetime_ref")
    meta = store.read_json(supervisor.session.meta_path)
    record = meta["crew"].get(crew_id) or {}
    if record.get("execution") != "supervised":
        raise CaptainError(f"[supervisor-crew] {crew_id} is not a supervised crew of this session.")
    bound_launch(supervisor, meta, crew_id, record, ref)
    adapter = ADAPTERS.get(record.get("provider"))
    if adapter is None:
        raise CaptainError(
            f"[supervisor-transport] {record.get('provider')} has no supervised transport."
        )
    link = Link(
        crew_id,
        ref,
        adapter,
        supervisor.session.events(f"{crew_id}-{record['incarnation_id']}"),
        model=record.get("model"),
        writable=tuple(record.get("outside_dirs") or ()),
    )
    link.relay = lambda payload: relay_hook(link, payload)
    link.ended = lambda event: turn_finished(supervisor, link, event)
    link.notice = lambda key, summary: post_notice(supervisor, link, key, summary)
    # One lock admits a crew and closes admission, so stop-all never misses one.
    with supervisor.lock:
        if supervisor.closing.is_set():
            raise CaptainError("[supervisor-closing] The supervisor is stopping every crew.")
        if crew_id in supervisor.links:
            raise CaptainError(f"[supervisor-crew] {crew_id} is already running.")
        supervisor.links[crew_id] = link
    threading.Thread(
        target=own, args=(supervisor, link), name=f"crew-{crew_id}", daemon=True
    ).start()
    if not link.ready.wait(config.number("supervisor", "startup_timeout")):
        link.stopping.set()
        raise CaptainError(f"[supervisor-startup] {crew_id} did not start before its deadline.")
    if link.error:
        # A refusal that already names its code (hooks untrusted, say) keeps it.
        if PROTOCOL_CODE.match(link.error):
            raise CaptainError(link.error)
        raise CaptainError(f"[supervisor-startup] {crew_id} did not start: {link.error}")
    return {"crew": crew_id, "state": "started"}


def stop_link(link, deadline):
    """outcome: stopped, retained (lifetime receipt not confirmed) or pending (owner still busy)."""
    link.stopping.set()
    if not link.done.wait(max(0, deadline - time.monotonic())):
        return {"crew": link.crew_id, "outcome": "pending", "detail": "owner still stopping"}
    if link.receipt is not None and link.receipt.ok:
        return {"crew": link.crew_id, "outcome": "stopped", "detail": link.receipt.reason}
    detail = link.error or getattr(link.receipt, "reason", "unknown")
    return {"crew": link.crew_id, "outcome": "retained", "detail": detail}


def stop_all(supervisor):
    """Stop every crew against one shared deadline; one outcome per crew."""
    deadline = time.monotonic() + config.number("supervisor", "stop_timeout")
    with supervisor.lock:
        links = list(supervisor.links.values())
    for link in links:
        link.stopping.set()
    return [stop_link(link, deadline) for link in links]


def stop(supervisor, params):
    crew_id = _text(params, "crew")
    with supervisor.lock:
        link = supervisor.links.get(crew_id)
    if link is None:
        return {"crew": crew_id, "outcome": "not-running", "detail": "nothing was stopped"}
    return stop_link(link, time.monotonic() + config.number("supervisor", "stop_timeout"))


def shutdown(supervisor, params):
    """App quit: refuse new crew, stop every crew, then end the supervisor after replying."""
    with supervisor.lock:
        supervisor.closing.set()
    return {"crews": stop_all(supervisor)}


def state(supervisor, params):
    crew_id = _text(params, "crew")
    with supervisor.lock:
        link = supervisor.links.get(crew_id)
    if link is None:
        return {"crew": crew_id, "state": None}
    return {
        "crew": crew_id,
        "state": link.adapter.state(link) if link.ready.is_set() else "working",
    }


def ring(supervisor, params):
    link = _link(supervisor, params)
    landed = link.adapter.ring(link, _text(params, "message_id"), _text(params, "text"))
    return {"crew": link.crew_id, **landed}


def interrupt(supervisor, params):
    link = _link(supervisor, params)
    return {"crew": link.crew_id, "outcome": link.adapter.interrupt(link, params.get("reason"))}


def set_model(supervisor, params):
    link = _link(supervisor, params)
    return {"crew": link.crew_id, **link.adapter.set_model(link, _text(params, "model"))}


def interrupt_all(supervisor, params):
    """Stop every working crew's turn in one call; one outcome per running crew."""
    reason = params.get("reason") if isinstance(params.get("reason"), str) else None
    with supervisor.lock:
        links = [
            link
            for link in supervisor.links.values()
            if link.ready.is_set() and not link.stopping.is_set() and not link.error
        ]
    results = {}

    def one(link):
        try:
            idle = link.adapter.state(link) == "idle"
            outcome = "idle" if idle else link.adapter.interrupt(link, reason)
            results[link.crew_id] = {"crew": link.crew_id, "outcome": outcome}
        except (CaptainError, OSError) as exc:
            results[link.crew_id] = {"crew": link.crew_id, "outcome": "error", "detail": str(exc)}

    # In parallel, so one wedged crew costs the call one request_timeout, not one per crew.
    threads = [threading.Thread(target=one, args=(link,), daemon=True) for link in links]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + config.number("supervisor", "request_timeout")
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    outcomes = []
    for link in links:
        outcome = results.get(link.crew_id) or {"crew": link.crew_id, "outcome": "pending"}
        if outcome["outcome"] == "interrupted":
            agent = crew_of(supervisor, link.crew_id).record["agent"]
            add_memory(supervisor.session.graph, agent, "interrupted", reason or "captain")
        outcomes.append(outcome)
    return {"crews": outcomes}


def resolve_grant(supervisor, params):
    """Answer a grant request: grant its exact command once, or deny it with a reason."""
    request_id = _text(params, "request")
    reason = params.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise CaptainError("[supervisor-params] reason must be a string.")
    recorded = store.read_json(supervisor.session.directory / "protocol.json")
    crew_id = next(
        (
            crew_id
            for crew_id, assignment_id in recorded["active"].items()
            if any(
                request["id"] == request_id
                for request in recorded["assignments"][assignment_id].get("grant_requests", ())
            )
        ),
        None,
    )
    if crew_id is None:
        raise CaptainError(f"[grant-request-unknown] No live assignment holds {request_id}.")
    crew = crew_of(supervisor, crew_id)
    if not crew.is_supervised:
        raise CaptainError(f"[supervisor-crew] {crew_id} is not a supervised crew.")
    with supervision.acting_as(supervisor.token):
        return protocol.resolve_request(crew, request_id, _text(params, "decision"), reason)


# Method -> handler(supervisor, params) returning a JSON object; a refusal is a CaptainError.
# Session-wide methods (shutdown, interrupt_all, resolve_grant) and per-crew ones (params.crew)
# share the one shape.
METHODS = {
    "start": start,
    "stop": stop,
    "shutdown": shutdown,
    "state": state,
    "ring": ring,
    "interrupt": interrupt,
    "interrupt_all": interrupt_all,
    "set_model": set_model,
    "resolve_grant": resolve_grant,
}


def relay_hook(link, payload):
    """(returncode, output) of a subprocess `captain hook`, which events.py assumes."""
    timeout = config.number("supervisor", "hook_timeout")
    command = instructions.hook_command(link.events)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            key: value
            for key, value in {**os.environ, **link.env}.items()
            if key != supervision.TOKEN_ENV
        },
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(json.dumps(payload).encode(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        processes.terminate_process_group(process)
        raise CaptainError(f"[supervisor-hook] captain hook exceeded {timeout}s.") from exc
    if not stdout.strip():
        return process.returncode, None
    try:
        output = json.loads(stdout)
    except ValueError:
        output = None
    if not isinstance(output, dict):
        raise CaptainError(
            f"[supervisor-hook] captain hook printed non-object output: {stdout[:2000]!r}"
        )
    return process.returncode, output


class Handler(socketserver.BaseRequestHandler):
    """One token-checked request per connection, answered within request_timeout."""

    def handle(self):
        supervisor = self.server.supervisor
        fd = self.request.fileno()
        timeout = config.number("supervisor", "request_timeout")
        identifier, ends = None, False
        try:
            message = jsonrpc.LineReader(fd).read(time.monotonic() + timeout)
            if message is None:
                return
            identifier = message.get("id")
            params = message.get("params")
            if "method" not in message or identifier is None or not isinstance(params, dict):
                raise CaptainError("[jsonrpc-malformed] Expected one request with object params.")
        except (CaptainError, OSError) as exc:
            reply = {"error": refusal(str(exc), INVALID_REQUEST)}
        else:
            reply = self.answer(supervisor, message["method"], params)
            ends = message["method"] == "shutdown" and "result" in reply
        try:
            # A fresh deadline: start and stop may outlast request_timeout by design.
            jsonrpc.write(
                fd, {"jsonrpc": "2.0", "id": identifier, **reply}, time.monotonic() + timeout
            )
        except (CaptainError, OSError):
            pass  # the client gave up; its own deadline already reported the failure
        if ends:
            supervisor.stopped.set()

    @staticmethod
    def answer(supervisor, method, params):
        token = params.pop("token", None)
        if not isinstance(token, str) or not hmac.compare_digest(token, supervisor.token):
            return {"error": refusal("[supervisor-token] Wrong or missing token.", TOKEN_ERROR)}
        if method not in METHODS:
            return {"error": refusal(f"[unknown-method] {method}", UNKNOWN_METHOD)}
        try:
            return {"result": METHODS[method](supervisor, params)}
        except (CaptainError, OSError) as exc:
            return {"error": refusal(str(exc))}


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def open_session(project, session_id):
    """Create or open a session with no pane and mark it supervised; Herdr sessions are refused."""
    current = session(project, None, session_id, create=session_id is None)
    with crew_meta(current.directory) as meta:
        if meta.get("workspace"):
            raise CaptainError(
                f"[supervisor-herdr-session] Session {meta['id']} belongs to a Herdr workspace; "
                "supervise a new session instead."
            )
        meta["transport"] = "supervisor"
    return current._replace(meta=meta)


def _remove_stale(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise CaptainError(f"[supervisor-socket-path] {path} exists and is not our socket.")
    path.unlink()


def serve(current, out, stopped):
    """Serve until `stopped` (a signal or `shutdown`), then stop every crew."""
    path = supervision.socket_path(current.directory)
    with ExitStack() as stack:
        try:
            stack.enter_context(store.lock(current.directory / supervision.LOCK, timeout=LOCK_WAIT))
        except CaptainError as exc:
            raise CaptainError(
                f"[supervisor-running] A supervisor already runs session {current.meta['id']}."
            ) from exc
        store.private_dir(path.parent)
        _remove_stale(path)
        server = Server(str(path), Handler)
        stack.callback(path.unlink, missing_ok=True)
        stack.callback(server.server_close)
        os.chmod(path, 0o600)
        supervisor = Supervisor(current, secrets.token_urlsafe(32), stopped)
        server.supervisor = supervisor
        threading.Thread(target=server.serve_forever, name="supervisor", daemon=True).start()
        try:
            print(
                json.dumps(
                    {
                        "type": "ready",
                        "session_id": current.meta["id"],
                        "socket": str(path),
                        "token": supervisor.token,
                        "pid": os.getpid(),
                    }
                ),
                file=out,
                flush=True,
            )
            while not stopped.wait(protocol.DRAIN_INTERVAL):
                drain_all(supervisor)
        finally:
            with supervisor.lock:
                supervisor.closing.set()
            server.shutdown()
            stop_all(supervisor)


def run(args, project):
    # Every guard, crew and hook inherits this environment; the token lives in memory only.
    os.environ.pop(supervision.TOKEN_ENV, None)
    current = open_session(project, args.session)
    stopped = threading.Event()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, lambda *_: stopped.set())
        serve(current, sys.stdout, stopped)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
