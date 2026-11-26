"""`captain supervise`: the one process per session that owns supervised crew behind a socket."""

import contextlib
import hmac
import json
import os
import queue
import re
import secrets
import select
import selectors
import signal
import socketserver
import stat
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    __version__,
    claude_agent,
    codex_app_server,
    config,
    eventlog,
    instructions,
    jsonrpc,
    lifetime,
    pi_rpc,
    processes,
    protocol,
    store,
    supervision,
    transcripts,
)
from .crew import Crew
from .memory import add_memory
from .runtime import CaptainError
from .sessions import Session, catalog, crew_meta, rename_session, session

# Provider -> adapter module (open, ring, interrupt, set_model, state over a Link).
# A provider without a row is refused at start.
ADAPTERS = {"codex": codex_app_server, "claude": claude_agent, "pi": pi_rpc}
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
PROTOCOL_VERSION = "captain.supervisor/1"
# Per provider, limits stated plainly; hello adds `supervised` from ADAPTERS (docs/supervisor.md).
CAPABILITIES = {
    "codex": {
        "transport": "app-server",
        "delivery": "body",
        "gate": "hooks",
        "output": "items",
        "limits": [
            "Needs Codex 0.164.0 or newer, with captain's four hooks trusted once in a Herdr "
            "Codex crew; Captain never writes Codex trust.",
            "Native ask-user requests are declined; the crew uses captain ask.",
        ],
    },
    "claude": {
        "transport": "sdk",
        "delivery": "hook",
        "gate": "hooks",
        "output": "turns",
        "limits": [
            "Needs the optional claude-agent-sdk: pip install captain-barbossa[app].",
            "Streams turn state only, not assistant or tool deltas.",
        ],
    },
    "pi": {
        "transport": "rpc",
        "delivery": "body",
        "gate": "extension",
        "output": "events",
        "limits": [
            "pi extension tools outside the tool map pass the gate.",
            "A model switch needs a provider/model id.",
        ],
    },
    "grok": {
        "transport": None,
        "delivery": "body",
        "gate": "none",
        "output": None,
        "limits": [
            "No supervised transport yet; recruit grok crew in a Herdr session.",
            "No hook: grok's own auto mode is the whole gate, so the gate fails open.",
        ],
    },
}
# Captain verbs `command` runs, with deadlines in seconds; `do` waits for an authorization flow.
COMMANDS = {
    "crew": 180,
    "assign": 60,
    "tell": 60,
    "answer": 60,
    "grant": 30,
    "interrupt": 60,
    "model": 60,
    "wait": 600,
    "status": 30,
    "dismiss": 120,
    "recover": 30,
    "memory": 60,
    "result": 120,
    "worktree": 120,
    "dashboard": 30,
    "inspect": 60,
}
# Bytes of a command's stdout or stderr returned; a reply line is capped at jsonrpc.MAX_LINE.
COMMAND_OUTPUT = 1024 * 1024
# Bytes read past COMMAND_OUTPUT so a secret straddling the cut is whole when redacted.
REDACT_MARGIN = 4096
# Crew identity a command must not inherit: the app acts as the captain.
CREW_ENV = (
    "CAPTAIN_ROLE",
    "CAPTAIN_CREW",
    "CAPTAIN_INCARNATION",
    "CAPTAIN_ASSIGNMENT",
    "CAPTAIN_WORKTREE_ID",
    "CAPTAIN_CREW_LAUNCHER",
)
NOTIFICATIONS = ("event", "delivery", "output", "attention")
# Subscription pacing: event re-reads, delivery projections and bytes of one file per notification.
STREAM_INTERVAL = 0.5
DELIVERY_INTERVAL = 2.0
STREAM_BYTES = 1024 * 1024
# Ephemeral notifications a slow subscriber may fall behind by before newer ones are dropped.
FEED_LIMIT = 1000
STREAM_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


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
    feeds: list = field(default_factory=list)


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
            if link.peer is not None:
                tee_output(supervisor, link)
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
                closer = getattr(link.adapter, "close", None)
                if closer is not None:
                    with contextlib.suppress(CaptainError, OSError):
                        closer(link)  # pi aborts a live turn first
                link.peer.close()
            elif link.stdin is not None:
                os.close(link.stdin)
                link.stdin = None
            job.shutdown()
            link.receipt = job.finish()
    except (CaptainError, OSError) as exc:
        link.error = str(exc)
    finally:
        if link.ready.is_set() and not link.stopping.is_set():
            lifecycle(supervisor, link.crew_id, "exited", link.error or "native CLI exited")
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


def publish(supervisor, method, params):
    """Hand an ephemeral notification to every subscription; a full feed drops its oldest."""
    with supervisor.lock:
        feeds = list(supervisor.feeds)
    for feed in feeds:
        for _ in range(3):
            try:
                feed.put_nowait((method, params))
                break
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    feed.get_nowait()


def tee_output(supervisor, link):
    """Publish the native's notifications as `output` before its adapter handles them."""
    handler = link.peer.handler

    def tee(message):
        if "id" not in message:
            publish(
                supervisor,
                "output",
                {
                    "crew": link.crew_id,
                    "method": message["method"],
                    "params": message.get("params"),
                },
            )
        handler(message)

    link.peer.handler = tee


def attention(supervisor, crew_id, message):
    match = PROTOCOL_CODE.match(message)
    code = match[1] if match else "supervisor-error"
    publish(supervisor, "attention", {"crew": crew_id, "code": code, "message": message})


def lifecycle(supervisor, crew_id, state, detail):
    """Record a crew's lifecycle change as a protocol action, best effort."""
    try:
        assignment = protocol.active_assignment(crew_of(supervisor, crew_id))
        eventlog.record_action(
            supervisor.session.directory,
            "lifecycle",
            crew_id,
            assignment["id"] if assignment else None,
            supervised=True,
            state=state,
            detail=detail,
        )
    except (CaptainError, OSError, KeyError):
        pass  # the assignment's own close still records a crew that is gone


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
    attention(supervisor, link.crew_id, summary)
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
    failure = None
    if not link.ready.wait(config.number("supervisor", "startup_timeout")):
        link.stopping.set()
        failure = f"[supervisor-startup] {crew_id} did not start before its deadline."
    elif link.error:
        # A refusal that already names its code (hooks untrusted, say) keeps it.
        failure = (
            link.error
            if PROTOCOL_CODE.match(link.error)
            else f"[supervisor-startup] {crew_id} did not start: {link.error}"
        )
    if failure:
        lifecycle(supervisor, crew_id, "failed", failure)
        attention(supervisor, crew_id, failure)
        raise CaptainError(failure)
    lifecycle(supervisor, crew_id, "started", record.get("provider"))
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
    return [stopped(supervisor, stop_link(link, deadline), "supervisor stopping") for link in links]


def stopped(supervisor, outcome, reason):
    """Record a stop outcome (stopped, retained or pending) as a lifecycle action."""
    lifecycle(supervisor, outcome["crew"], outcome["outcome"], reason or outcome["detail"])
    return outcome


def stop(supervisor, params):
    crew_id = _text(params, "crew")
    with supervisor.lock:
        link = supervisor.links.get(crew_id)
    if link is None:
        return {"crew": crew_id, "outcome": "not-running", "detail": "nothing was stopped"}
    deadline = time.monotonic() + config.number("supervisor", "stop_timeout")
    reason = params.get("reason") if isinstance(params.get("reason"), str) else None
    return stopped(supervisor, stop_link(link, deadline), reason)


def shutdown(supervisor, params):
    """App quit, or with `end` the user ending the session: stop every crew, then exit."""
    end = params.get("end", False)
    if type(end) is not bool:
        raise CaptainError("[supervisor-params] end must be true or false.")
    with supervisor.lock:
        supervisor.closing.set()
    crews = stop_all(supervisor)
    if end:
        with crew_meta(supervisor.session.directory) as meta:
            meta["ended_at"] = time.time()
    return {"crews": crews, "ended": end}


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


def hello(supervisor, params):
    return {
        "protocol": PROTOCOL_VERSION,
        "version": __version__,
        "session_id": supervisor.session.meta["id"],
        "project": supervisor.session.meta["project"],
        "capabilities": {
            provider: {"supervised": provider in ADAPTERS, **row}
            for provider, row in CAPABILITIES.items()
        },
        "methods": sorted(METHODS),
        "notifications": list(NOTIFICATIONS),
        "commands": sorted(COMMANDS),
    }


def _capture(process, timeout):
    """(stdout, stderr) of a running process, each kept to COMMAND_OUTPUT plus a redaction margin."""
    deadline = time.monotonic() + timeout
    kept = {process.stdout: bytearray(), process.stderr: bytearray()}
    with selectors.DefaultSelector() as selector:
        for pipe in kept:
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                buffer = kept[key.fileobj]
                buffer += chunk[: max(COMMAND_OUTPUT + REDACT_MARGIN - len(buffer), 0)]
    process.wait(max(0, deadline - time.monotonic()))
    for pipe in kept:
        pipe.close()
    return bytes(kept[process.stdout]), bytes(kept[process.stderr])


def _clip(data, literals):
    """Redact before clipping, so no secret is cut into a visible prefix."""
    text = eventlog.redact_text(data.decode("utf-8", "replace"), literals)
    return eventlog.redact_text(text[:COMMAND_OUTPUT]), len(text) > COMMAND_OUTPUT


def command(supervisor, params):
    """Run one whitelisted captain verb in this session as the captain: {exit, stdout, stderr}."""
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        raise CaptainError("[supervisor-params] argv must be a non-empty list of strings.")
    verb = argv[0]
    if verb not in COMMANDS:
        raise CaptainError(f"[command-refused] {verb!r} is not a whitelisted captain verb.")
    if verb == "dashboard" and argv[1:] != ["--once"]:
        raise CaptainError("[command-refused] dashboard runs only as `dashboard --once`.")
    meta = supervisor.session.meta
    env = {key: value for key, value in os.environ.items() if key not in CREW_ENV}
    env.update(
        CAPTAIN_PROJECT=meta["project"],
        CAPTAIN_SESSION=meta["id"],
        **{supervision.TOKEN_ENV: supervisor.token},
    )
    timeout = COMMANDS[verb]
    try:
        process = subprocess.Popen(
            [instructions.python_command(), "-m", "captain_barbossa", "--session", meta["id"]]
            + argv,
            cwd=meta["project"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise CaptainError(f"[command-failed] {verb} could not run: {exc}") from exc
    try:
        stdout, stderr = _capture(process, timeout)
    except subprocess.TimeoutExpired as exc:
        processes.terminate_process_group(process)
        raise CaptainError(f"[command-timeout] {verb} exceeded {timeout}s.") from exc
    literals = (supervisor.token,)
    (out, clipped), (err, err_clipped) = _clip(stdout, literals), _clip(stderr, literals)
    reply = {"exit": process.returncode, "stdout": out, "stderr": err}
    if clipped or err_clipped:
        reply["truncated"] = True
    return reply


def current_session(supervisor):
    """The session with its roster as now on disk; crew join after the supervisor starts."""
    directory = supervisor.session.directory
    return Session(directory, store.read_json(directory / "session.json"))


def transcript(supervisor, params):
    limit = params.get("limit", transcripts.DEFAULT_LIMIT)
    page = transcripts.read(
        current_session(supervisor),
        _text(params, "agent"),
        after=params.get("after"),
        before=params.get("before"),
        limit=limit,
        incarnation=params.get("incarnation"),
    )
    return eventlog.redact(page, (supervisor.token,))


def step(supervisor, params):
    entry = transcripts.step(
        current_session(supervisor),
        _text(params, "agent"),
        _text(params, "id"),
        incarnation=params.get("incarnation"),
    )
    return eventlog.redact(entry, (supervisor.token,))


def rename(supervisor, params):
    name = _text(params, "name")
    try:
        rename_session(supervisor.session.directory, name)
    except CaptainError as exc:
        raise CaptainError(f"[session-name] {exc}") from exc
    return {"session_id": supervisor.session.meta["id"], "name": name.strip()}


def sessions(supervisor, params):
    return catalog(Path(supervisor.session.meta["project"]))


def subscribe(supervisor, params):
    """Validate the cursors; Handler then streams notifications on this connection."""
    cursors = params.get("cursors", {})
    if not isinstance(cursors, dict):
        cursors = None
    normal = {}
    for name, held in (cursors or {}).items():
        held = {"offset": held, "generation": None} if type(held) is int else held
        if not (
            isinstance(name, str)
            and STREAM_NAME.fullmatch(name)
            and isinstance(held, dict)
            and type(held.get("offset")) is int
            and held["offset"] >= 0
            and (held.get("generation") is None or isinstance(held["generation"], str))
        ):
            cursors = None
            break
        normal[name] = {"offset": held["offset"], "generation": held.get("generation")}
    if cursors is None:
        raise CaptainError(
            "[supervisor-params] cursors must map stream names to {offset, generation} or an "
            "offset."
        )
    return {"session_id": supervisor.session.meta["id"], "cursors": normal}


def streams(supervisor):
    """Stream name -> path: the protocol action log and every native event file."""
    directory = supervisor.session.directory
    found = {"actions": directory / eventlog.ACTIONS}
    found.update((path.stem, path) for path in sorted((directory / "events").glob("*.jsonl")))
    return found


def replay(supervisor, cursors):
    """`event` notifications past each cursor; a replaced or truncated file replays with `reset`."""
    with contextlib.suppress(CaptainError, OSError):
        eventlog.flush_actions(supervisor.session.directory)
    roster = store.read_json(supervisor.session.meta_path).get("crew", {})
    for name, path in streams(supervisor).items():
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        generation = f"{info.st_dev}-{info.st_ino}"
        held = cursors.get(name) or {"offset": 0, "generation": None}
        reset = held["generation"] not in (None, generation) or held["offset"] > info.st_size
        cursors[name] = {"offset": 0 if reset else held["offset"], "generation": generation}
        crew = None if name == "actions" else eventlog.match_crew(name, roster)[0]
        while True:
            start = cursors[name]["offset"]
            events, offset = eventlog.read_events(path, start, max_bytes=STREAM_BYTES)
            cursors[name]["offset"] = offset
            if events or reset:
                yield (
                    "event",
                    {
                        "stream": name,
                        "crew": crew,
                        "cursor": offset,
                        "generation": generation,
                        "reset": reset,
                        "events": events,
                    },
                )
            reset = False
            if offset == start:
                break


def deliveries(supervisor):
    """Pending deliveries as `wait --all` would return them, never acknowledged; None if unread."""
    try:
        with supervision.acting_as(supervisor.token):
            return protocol.wait_all(supervisor.session, 0)["deliveries"]
    except (CaptainError, OSError, KeyError):
        return None  # best effort: the next projection retries


def hung_up(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    for _, mask in poller.poll(0):
        if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
            return True
        try:
            return not os.read(fd, 65536)
        except BlockingIOError:
            return False
    return False


def join(supervisor):
    """A new subscription's feed, registered before its reply so no live output is missed."""
    feed = queue.Queue(FEED_LIMIT)
    with supervisor.lock:
        supervisor.feeds.append(feed)
    return feed


def leave(supervisor, feed):
    with supervisor.lock:
        supervisor.feeds.remove(feed)


def stream(supervisor, fd, cursors, feed):
    """Send redacted notifications until the client leaves or the supervisor stops."""
    timeout = config.number("supervisor", "request_timeout")
    sent, projected = set(), 0.0

    def send(method, params):
        message = {"jsonrpc": "2.0", "method": method}
        message["params"] = eventlog.redact(params, (supervisor.token,))
        try:
            jsonrpc.write(fd, message, time.monotonic() + timeout)
        except CaptainError as exc:
            if "[jsonrpc-line]" not in str(exc):
                raise  # an oversized notification is dropped; anything else ends the stream

    os.set_blocking(fd, False)
    try:
        while not supervisor.stopped.is_set() and not hung_up(fd):
            for method, params in replay(supervisor, cursors):
                send(method, params)
            if time.monotonic() - projected >= DELIVERY_INTERVAL:
                projected = time.monotonic()
                pending = deliveries(supervisor)
                keys = {}
                for delivery in pending or ():
                    key = delivery["delivery_id"] or json.dumps(delivery, sort_keys=True)
                    keys[key] = delivery
                for key, delivery in keys.items():
                    if key not in sent:
                        send("delivery", delivery)
                if pending is not None:
                    sent = set(keys)  # an acknowledged delivery leaves the projection
            items = []
            try:
                items.append(feed.get(timeout=STREAM_INTERVAL))
                while len(items) < FEED_LIMIT:
                    items.append(feed.get_nowait())
            except queue.Empty:
                pass
            for item in items:
                send(*item)
    except (CaptainError, OSError):
        pass  # the client left or stopped reading; nothing it holds needs undoing
    finally:
        leave(supervisor, feed)


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
    "hello": hello,
    "command": command,
    "subscribe": subscribe,
    "transcript": transcript,
    "step": step,
    "rename": rename,
    "sessions": sessions,
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
    """One token-checked request per connection, answered within request_timeout; a
    subscribe keeps the connection for its notifications."""

    def handle(self):
        supervisor = self.server.supervisor
        fd = self.request.fileno()
        timeout = config.number("supervisor", "request_timeout")
        identifier, ends, feed = None, False, None
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
            if message["method"] == "subscribe" and "result" in reply:
                feed = join(supervisor)
        try:
            # A fresh deadline: start and stop may outlast request_timeout by design.
            jsonrpc.write(
                fd, {"jsonrpc": "2.0", "id": identifier, **reply}, time.monotonic() + timeout
            )
        except (CaptainError, OSError):
            if feed is not None:  # the client gave up; its own deadline already reported it
                leave(supervisor, feed)
                feed = None
        if ends:
            supervisor.stopped.set()
        if feed is not None:
            stream(supervisor, fd, dict(reply["result"]["cursors"]), feed)

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
        meta.pop("ended_at", None)
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
        eventlog.hide(supervisor.token)
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
