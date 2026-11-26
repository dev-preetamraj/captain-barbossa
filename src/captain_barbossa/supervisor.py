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
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import (
    __version__,
    captain_bridge,
    claude_agent,
    codex_app_server,
    config,
    do,
    eventlog,
    instructions,
    jsonrpc,
    launch,
    lifetime,
    pi_rpc,
    processes,
    protocol,
    results,
    runtime,
    store,
    supervision,
    transcripts,
    usage,
)
from .crew import Crew, event_status
from .memory import add_memory
from .models import CAPABILITIES, PROVIDERS, TRANSPORTS, resolve_model
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
# Verbs `command` runs only after resolve_authorization approves them, with their deadlines.
AUTHORIZED = {"do": processes.TIMEOUT + 60, "quiet": processes.TIMEOUT + 60}
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
# Bytes of the captain's newest hook events read to find when its last turn ended.
CAPTAIN_TAIL = 65536
CAPTAIN = "captain"
# How long the bridge sleeps between rounds when no turn end or start wakes it.
BRIDGE_POLL = 2.0
# How long a native prompt of the captain's waits for the user before it is declined.
CAPTAIN_ASK_TIMEOUT = 1800
# Closed captain requests kept in protocol.json; open ones are always kept.
REQUESTS_KEPT = 200
# Hook events adapters relay to record a turn, never to ask anyone.
TURN_HOOKS = ("Notification", "Stop")
# Methods the captain's scoped token may call: exactly those the captain CLI uses.
CAPTAIN_METHODS = ("hello", "start", "stop", "state", "ring", "interrupt", "set_model")
STREAM_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The `output` method that carries a live usage frame, apart from any provider's own methods.
USAGE_OUTPUT = "captain/usage"
# How long a device-code sign-in may wait for the user, and the output lines it keeps.
SIGN_IN_TIMEOUT = 900
SIGN_IN_LINES = 20


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
    captain: bool = False
    ask: object = None
    usage: object = None


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
    captain: object = None
    statuses: dict = field(default_factory=dict)
    sign_ins: dict = field(default_factory=dict)
    # The captain's own token: its process tree may only act as the CLI does (CAPTAIN_METHODS).
    captain_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    bridge_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def literals(self):
        return (self.token, self.captain_token)


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
    link.usage = lambda frame: live_usage(supervisor, link, frame)
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
    # The captain first, so the bridge never rings it with the crew's own stop notices.
    stop_captain(supervisor)
    crews = stop_all(supervisor)
    cancel_sign_ins(supervisor)
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
        if (run := supervisor.captain) is not None and live(run.link):
            links.append(run.link)
    captain_bridge.add_note(
        supervisor.session.directory, text="The user interrupted every working crew and you."
    )
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
        if outcome["outcome"] == "interrupted" and not link.captain:
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
        reply = protocol.resolve_request(crew, request_id, _text(params, "decision"), reason)
    captain_bridge.add_note(
        supervisor.session.directory,
        text=f"The user {reply['state']} grant request {request_id} of {crew.display_name}.",
    )
    return reply


@dataclass
class CaptainRun:
    """The supervised captain: its link, process and the bridge's delivery bookkeeping."""

    link: Link
    provider: str
    transport: str
    process: object = None
    wake: threading.Event = field(default_factory=threading.Event)
    mine: set = field(default_factory=set)
    reported: set = field(default_factory=set)
    asks: dict = field(default_factory=dict)


def live(link):
    return link.ready.is_set() and not link.stopping.is_set() and not link.error


def captain_command(supervisor, provider, model, effort):
    """(argv, env, model, transport) of the captain's native CLI on its structured transport."""
    transport = next(iter(TRANSPORTS.get(provider, ())), None)
    if transport not in launch.CAPTAIN_TRANSPORTS or provider not in ADAPTERS:
        raise CaptainError(f"[captain-transport] {provider} cannot run as a supervised captain.")
    if effort is not None and (provider != "pi" or not isinstance(effort, str)):
        raise CaptainError("[supervisor-params] effort is a string, for a pi captain only.")
    current = current_session(supervisor)
    model = resolve_model(provider, model) if model else None
    argv, overlay = launch.prepare_launch(
        current,
        Path(current.meta["project"]),
        provider,
        runtime.executable(provider),
        model,
        transport=transport,
        bridge=False,
    )
    if effort:
        argv += ["--thinking", effort]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CAPTAIN_") or key == "CAPTAIN_MEMORY_ROOT"
    }
    env.update(overlay)
    # The captain drives the CLI, which reaches this supervisor only with the token.
    env[supervision.TOKEN_ENV] = supervisor.captain_token
    store.write_json(
        current.directory / "captain.json",
        {"provider": provider, "pane": None, "transport": "supervisor"},
    )
    return argv, env, model, transport


def own_captain(supervisor, run, argv, env):
    """Start the captain in its own process group, then hold it until stopped or exited."""
    link = run.link
    process = None
    try:
        native_in, link.stdin = os.pipe()
        link.stdout, native_out = os.pipe()
        errors, native_err = os.pipe()
        try:
            process = run.process = subprocess.Popen(
                argv,
                cwd=link.root,
                env=env,
                stdin=native_in,
                stdout=native_out,
                stderr=native_err,
                start_new_session=True,
            )
        finally:
            for fd in (native_in, native_out, native_err):
                os.close(fd)
            keep_stderr(supervisor, link, errors)
        link.adapter.open(link)
        if link.peer is not None:
            tee_output(supervisor, link)
        link.ready.set()
        run.wake.set()
        while process.poll() is None and not link.stopping.is_set():
            link.stopping.wait(0.2)
    except (CaptainError, OSError) as exc:
        link.error = str(exc)
    finally:
        if link.ready.is_set() and not link.stopping.is_set():
            lifecycle_captain(supervisor, "exited", link.error or "native CLI exited")
        closer = getattr(link.adapter, "close", None)
        if closer is not None and link.peer is not None:
            with contextlib.suppress(CaptainError, OSError):
                closer(link)
        if link.peer is not None:
            link.peer.close()
        for fd in (link.stdin, link.stdout):
            if fd is not None:
                os.close(fd)
        link.stdin = link.stdout = None
        if process is not None:
            # End of input asks a stdio server to exit; the group is killed past the deadline.
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(config.number("supervisor", "stop_timeout"))
            processes.terminate_process_group(process)
        link.ready.set()
        link.done.set()
        run.wake.set()


def lifecycle_captain(supervisor, state, detail):
    with contextlib.suppress(CaptainError, OSError):
        eventlog.record_action(
            supervisor.session.directory,
            "lifecycle",
            CAPTAIN,
            None,
            supervised=True,
            state=state,
            detail=detail,
        )


def captain_turn_ended(supervisor, run, event):
    """Record the turn for waitingOn and wake the bridge to deliver what the turn held up."""
    if event is not None:
        with contextlib.suppress(CaptainError, OSError):
            relay_hook(run.link, event)
    run.wake.set()


def captain_ask(supervisor, run, kind, payload):
    """Put a native prompt of the captain's to the user and wait for the answer, or None."""
    directory = supervisor.session.directory
    request = captain_bridge.open_request(directory, kind, payload)
    slot = run.asks[request["id"]] = [threading.Event(), None]
    attention(supervisor, CAPTAIN, f"[captain-request] {request['id']}: the captain asks ({kind}).")
    answered = slot[0].wait(CAPTAIN_ASK_TIMEOUT)
    run.asks.pop(request["id"], None)
    if not answered:
        with contextlib.suppress(CaptainError):
            captain_bridge.close_request(directory, request["id"], "expired")
        return None
    return slot[1]


def captain_relay(supervisor, run, event):
    """A captain's turn record goes to its own hook; a native approval request to the user."""
    turn = event.get("type") == "agent-turn-complete" or event.get("hook_event_name") in TURN_HOOKS
    return relay_hook(run.link, event) if turn else captain_approval(supervisor, run, event)


def captain_approval(supervisor, run, event):
    """A native approval request answered by the user, in the hook output shape adapters read."""
    inputs = event.get("tool_input") or {}
    payload = {
        "tool": event.get("tool_name"),
        "command": inputs.get("command"),
        "cwd": event.get("cwd"),
        "input": inputs,
    }
    decision = captain_ask(supervisor, run, "approval", payload)
    behavior = "allow" if decision == "approve" else "deny"
    return 0, {"hookSpecificOutput": {"decision": {"behavior": behavior}}}


def captain_question(supervisor, run, params):
    """A native ask-user prompt put to the user, answered in Codex's requestUserInput shape."""
    answers = captain_ask(supervisor, run, "question", params)
    return {key: {"answers": [value]} for key, value in (answers or {}).items()} or None


def bridge_captain(supervisor, run):
    """Deliver crew results, notes and authorization outcomes until the captain stops."""
    directory = supervisor.session.directory
    link = run.link

    def ring_captain(message_id, text):
        if not live(link):
            return {"landed": False, "gate": "captain stopped"}
        return link.adapter.ring(link, message_id, text)

    def ack(delivery):
        current = current_session(supervisor)
        with contextlib.suppress(CaptainError, KeyError):
            crew = Crew.resolve(current, delivery["crew"])
            with supervision.acting_as(supervisor.token):
                protocol.poll(crew, ack=delivery["delivery_id"])

    def report(code, message):
        if (code, message) not in run.reported:
            run.reported.add((code, message))
            attention(supervisor, CAPTAIN, f"[{code}] {message}")

    link.ready.wait()
    while live(link) and not supervisor.stopped.is_set():
        try:
            with supervision.acting_as(supervisor.token):
                pending = protocol.wait_all(supervisor.session, 0)["deliveries"]
            with supervisor.bridge_lock:
                captain_bridge.step(directory, pending, ring_captain, ack, run.mine, report)
        except (CaptainError, OSError, KeyError) as exc:
            report("captain-bridge", str(exc))
        run.wake.wait(BRIDGE_POLL)
        run.wake.clear()


def start_captain(supervisor, params):
    """Start the session's captain on its structured transport, fed by the delivery bridge."""
    provider = params.get("provider") or config.text("captain", "agent")
    model = params.get("model") or config.text("captain", "model")
    if not isinstance(provider, str) or (model is not None and not isinstance(model, str)):
        raise CaptainError("[supervisor-params] provider and model must be strings.")
    if supervisor.captain is not None and not supervisor.captain.link.done.is_set():
        raise CaptainError("[captain-running] The captain is already running.")
    argv, env, model, transport = captain_command(supervisor, provider, model, params.get("effort"))
    project = supervisor.session.meta["project"]
    expire_captain_requests(supervisor)
    with supervisor.lock:
        if supervisor.closing.is_set():
            raise CaptainError("[supervisor-closing] The supervisor is stopping.")
        if supervisor.captain is not None and not supervisor.captain.link.done.is_set():
            raise CaptainError("[captain-running] The captain is already running.")
        link = Link(
            CAPTAIN,
            None,
            ADAPTERS[provider],
            supervisor.session.events(CAPTAIN),
            env=env,
            root=project,
            model=model,
            captain=True,
        )
        run = supervisor.captain = CaptainRun(link, provider, transport)
    link.relay = lambda event: captain_relay(supervisor, run, event)
    link.ended = lambda event: captain_turn_ended(supervisor, run, event)
    link.notice = lambda key, summary: attention(supervisor, CAPTAIN, summary)
    link.ask = lambda kind, params: (
        captain_question(supervisor, run, params)
        if kind == "question"
        else captain_ask(supervisor, run, kind, params)
    )
    link.usage = lambda frame: live_usage(supervisor, link, frame)
    threading.Thread(
        target=own_captain, args=(supervisor, run, argv, env), name="captain", daemon=True
    ).start()
    if not link.ready.wait(config.number("supervisor", "startup_timeout")) or link.error:
        link.stopping.set()
        failure = link.error or "did not start before its deadline"
        message = failure if PROTOCOL_CODE.match(failure) else f"[captain-start] {failure}"
        lifecycle_captain(supervisor, "failed", message)
        raise CaptainError(message)
    threading.Thread(
        target=bridge_captain, args=(supervisor, run), name="captain-bridge", daemon=True
    ).start()
    lifecycle_captain(supervisor, "started", provider)
    return {"provider": provider, "transport": transport, "model": model, "state": "started"}


def _captain(supervisor):
    run = supervisor.captain
    if run is None or not live(run.link):
        raise CaptainError("[captain-not-running] No captain runs under this supervisor.")
    return run


def captain_state(supervisor, params):
    run = supervisor.captain
    if run is None or not live(run.link):
        return {"state": None}
    return {"state": run.link.adapter.state(run.link), "provider": run.provider}


def message_captain(supervisor, params):
    """The user's own prompt to the captain, with any operator notes in front."""
    run = _captain(supervisor)
    directory = supervisor.session.directory
    notes = captain_bridge.pending_notes(directory)
    text = captain_bridge.with_notes(_text(params, "text"), notes)
    rung = run.link.adapter.ring(run.link, f"user-{uuid4().hex}", text)
    if rung["landed"]:
        captain_bridge.notes_delivered(directory, notes)
    return rung


def interrupt_captain(supervisor, params):
    run = _captain(supervisor)
    return {"outcome": run.link.adapter.interrupt(run.link, params.get("reason"))}


def stop_captain(supervisor, params=None):
    """Stop the captain: end of input first, then its process group past stop_timeout."""
    run = supervisor.captain
    if run is None or run.link.done.is_set():
        return {"outcome": "not-running"}
    run.link.stopping.set()
    run.wake.set()
    deadline = 2 * config.number("supervisor", "stop_timeout") + processes.GROUP_TERMINATE_TIMEOUT
    if not run.link.done.wait(deadline):
        return {"outcome": "pending"}
    expire_captain_requests(supervisor, run)
    lifecycle_captain(supervisor, "stopped", "stopped by the supervisor")
    return {"outcome": "stopped"}


def expire_captain_requests(supervisor, run=None):
    """Expire requests no running captain waits on and keep the newest closed ones."""
    directory = supervisor.session.directory
    if run is not None:
        for slot in list(run.asks.values()):
            slot[0].set()  # an unanswered prompt is declined
    path = directory / "protocol.json"
    rows = store.read_json(path).get("captain_requests", []) if path.exists() else []
    for row in rows:
        if row["state"] == "open":
            with contextlib.suppress(CaptainError):
                captain_bridge.close_request(directory, row["id"], "expired")
    if len(rows) > REQUESTS_KEPT:
        with protocol.checkpoint(directory) as state:
            rows = state.get("captain_requests", [])
            closed = [row for row in rows if row["state"] != "open"][-REQUESTS_KEPT:]
            keep = {row["id"] for row in closed}
            state["captain_requests"] = [
                row for row in rows if row["state"] == "open" or row["id"] in keep
            ]


def resolve_captain_delivery(supervisor, params):
    """The user's decision on a delivery an earlier supervisor sent without confirmation."""
    with supervisor.bridge_lock:
        reply = captain_bridge.resolve_claim(
            supervisor.session.directory, _text(params, "delivery_id"), params.get("decision")
        )
    if (run := supervisor.captain) is not None:
        run.wake.set()
    return reply


def captain_requests(supervisor, params):
    path = supervisor.session.directory / "protocol.json"
    rows = store.read_json(path).get("captain_requests", []) if path.exists() else []
    return {"requests": rows}


def answer_captain(supervisor, params):
    """Answer one of the captain's native prompts that the supervisor put to the user."""
    run = _captain(supervisor)
    request_id = _text(params, "request")
    slot = run.asks.get(request_id)
    if slot is None:
        raise CaptainError(f"[captain-request-closed] Request {request_id} is not waiting.")
    directory = supervisor.session.directory
    rows = store.read_json(directory / "protocol.json").get("captain_requests", [])
    kind = next((row["kind"] for row in rows if row["id"] == request_id), None)
    answer = captain_bridge.answer_for(kind, params)
    captain_bridge.close_request(directory, request_id, "answered", answer)
    slot[1] = answer
    slot[0].set()
    return {"request": request_id, "state": "answered"}


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
        "authorized_commands": sorted(AUTHORIZED),
    }


def live_usage(supervisor, link, frame):
    """Keep a live reading beside the agent's events and publish it; None re-reads the native's."""
    try:
        if frame is not None:
            usage.record_live(link.events, frame)
        else:
            frame = usage.with_live(link.events)
    except (CaptainError, OSError):
        return  # best effort: the next reading replaces it
    publish(supervisor, "output", {"crew": link.crew_id, "method": USAGE_OUTPUT, "params": frame})


def agent_usage(supervisor, params):
    """Each current agent's usage: the native reading, with live readings filling its gaps."""
    current = current_session(supervisor)
    agents = (
        [(CAPTAIN, current.events(CAPTAIN))]
        if (current.directory / "captain.json").exists()
        else []
    )
    agents += [
        (crew.crew_id, crew.events) for crew in Crew.members(current) if not crew.is_dismissed
    ]
    rows = []
    for agent, events in agents:
        try:
            frame = usage.with_live(events)
        except (CaptainError, OSError, ValueError, KeyError, TypeError):
            frame = None  # unreadable reads as unknown, never zero
        rows.append({"agent": agent, "usage": frame})
    return {"agents": rows}


def probe(provider, root):
    """One provider's install, version and sign-in status, each None when it cannot be read."""
    timeout = config.number("supervisor", "startup_timeout")
    row = {
        "provider": provider,
        "installed": False,
        "version": None,
        "signed_in": None,
        "account": None,
        "detail": None,
        "checked_at": time.time(),
    }
    try:
        binary = runtime.executable(provider)
    except CaptainError as exc:
        row["detail"] = str(exc)
        return row
    row["installed"] = True
    with contextlib.suppress(CaptainError, OSError):
        result = processes.capture(root, [binary, "--version"], timeout)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        row["version"] = lines[0] if result.returncode == 0 and lines else None
    if provider not in ADAPTERS:
        return row
    try:
        row.update(ADAPTERS[provider].account(binary, root, timeout))
    except (CaptainError, OSError) as exc:
        row["detail"] = str(exc)
    return row


def check_providers(supervisor, names):
    """Probe providers in parallel and cache the result; one past its deadline reads as unknown."""
    root, found = supervisor.session.meta["project"], {}
    threads = [
        threading.Thread(
            target=lambda name: found.update({name: probe(name, root)}), args=(name,), daemon=True
        )
        for name in names
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2 * config.number("supervisor", "startup_timeout")
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    late = "[provider-check] The check did not finish before its deadline."
    with supervisor.lock:
        for name in names:
            supervisor.statuses[name] = dict(found.get(name) or {"provider": name, "detail": late})


def providers(supervisor, params):
    """Each provider's status, cached until `recheck`, with its sign-in routes and attempt."""
    recheck, wanted = params.get("recheck", False), params.get("provider")
    if type(recheck) is not bool:
        raise CaptainError("[supervisor-params] recheck must be true or false.")
    if wanted is not None and wanted not in PROVIDERS:
        raise CaptainError(f"[supervisor-params] provider must be one of {', '.join(PROVIDERS)}.")
    names = [wanted] if wanted else list(PROVIDERS)
    with supervisor.lock:
        stale = [name for name in names if recheck or name not in supervisor.statuses]
    if stale:
        check_providers(supervisor, stale)
    with supervisor.lock:
        attempts = {name: supervisor.sign_ins.get(name) for name in names}
        statuses = {name: supervisor.statuses[name] for name in names}
    rows = []
    for name in names:
        unknown = dict.fromkeys(("installed", "version", "signed_in", "account", "checked_at"))
        rows.append(
            {
                **unknown,
                **statuses[name],
                "sign_in": CAPABILITIES[name]["sign_in"],
                "attempt": attempts[name].view() if attempts[name] else None,
            }
        )
    return {"providers": rows}


@dataclass
class SignIn:
    """A sign-in the user started; its `literals` (an API key) are redacted from all it reports."""

    provider: str
    method: str
    announce: object
    literals: tuple = ()
    state: str = "starting"
    user_code: str | None = None
    verification_url: str | None = None
    detail: str | None = None
    output: list = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    changed: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    def report(self, line=None, **fields):
        """Record progress; a change of any field is announced, a bare output line is kept."""
        if line is not None:
            line = eventlog.redact_text(line, self.literals)
            self.output = [*self.output, line][-SIGN_IN_LINES:]
        for key, value in fields.items():
            if isinstance(value, str):
                value = eventlog.redact_text(value, self.literals)
            setattr(self, key, value)
        if fields:
            self.changed.set()
            self.announce(self)

    def view(self):
        return {
            "provider": self.provider,
            "method": self.method,
            "state": self.state,
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "detail": self.detail,
            "output": list(self.output),
        }


def announce(supervisor, attempt):
    """Publish a sign-in's progress as an `attention` the app shows, carrying the attempt."""
    view, name = attempt.view(), attempt.provider
    if attempt.state == "waiting":
        code = f" and enter {view['user_code']}" if view["user_code"] else ""
        message = f"Sign {name} in: open {view['verification_url']}{code}."
    elif attempt.state == "signed-in":
        message = f"{name} is signed in."
    elif attempt.state == "failed":
        message = f"{name} sign-in failed: {view['detail']}"
    else:
        message = f"{name} sign-in is {attempt.state}."
    params = {"crew": None, "code": "sign-in", "message": f"[sign-in] {message}", "sign_in": view}
    publish(supervisor, "attention", params)


def sign_in(supervisor, params):
    """Start a provider's sign-in; a provider Captain cannot sign in returns its guidance."""
    provider = _text(params, "provider")
    if provider not in CAPABILITIES:
        raise CaptainError(f"[supervisor-params] provider must be one of {', '.join(PROVIDERS)}.")
    routes = CAPABILITIES[provider]["sign_in"]
    if not routes["methods"]:
        return {"provider": provider, "state": "guidance", "guidance": routes["guidance"]}
    method = params.get("method") or routes["methods"][0]
    if method not in routes["methods"]:
        raise CaptainError(
            f"[sign-in-method] {provider} signs in with {' or '.join(routes['methods'])}."
        )
    secret = params.get("api_key") if method == "apiKey" else None
    if method == "apiKey" and (not isinstance(secret, str) or not secret.strip()):
        raise CaptainError("[supervisor-params] api_key must be a non-empty string.")
    try:
        binary = runtime.executable(provider)
    except CaptainError as exc:
        raise CaptainError(f"[sign-in-missing] {exc}") from exc
    attempt = SignIn(
        provider, method, lambda found: announce(supervisor, found), (secret,) if secret else ()
    )
    with supervisor.lock:
        if supervisor.closing.is_set():
            raise CaptainError("[supervisor-closing] The supervisor is stopping.")
        running = supervisor.sign_ins.get(provider)
        if running is not None and not running.done.is_set():
            raise CaptainError(f"[sign-in-running] A {provider} sign-in is already under way.")
        supervisor.sign_ins[provider] = attempt
    work = {"method": method, "api_key": secret}
    threading.Thread(
        target=run_sign_in, args=(supervisor, attempt, binary, work), daemon=True
    ).start()
    attempt.changed.wait(config.number("supervisor", "startup_timeout"))
    return attempt.view()


def run_sign_in(supervisor, attempt, binary, work):
    """Run the adapter's sign-in to its end; a success refreshes the cached status first."""
    deadline = time.monotonic() + SIGN_IN_TIMEOUT
    root = supervisor.session.meta["project"]
    try:
        state, detail = ADAPTERS[attempt.provider].sign_in(attempt, binary, root, work, deadline)
    except (CaptainError, OSError) as exc:
        state, detail = "failed", str(exc)
    if state == "signed-in":
        check_providers(supervisor, [attempt.provider])
    attempt.report(state=state, detail=detail)
    attempt.done.set()


def cancel_sign_in(supervisor, params):
    provider = _text(params, "provider")
    with supervisor.lock:
        attempt = supervisor.sign_ins.get(provider)
    if attempt is None or attempt.done.is_set():
        return {"provider": provider, "state": "not-running"}
    attempt.cancel.set()
    attempt.done.wait(config.number("supervisor", "stop_timeout"))
    return attempt.view()


def cancel_sign_ins(supervisor):
    """Cancel every sign-in under way against one stop deadline."""
    with supervisor.lock:
        attempts = list(supervisor.sign_ins.values())
    for attempt in attempts:
        attempt.cancel.set()
    deadline = time.monotonic() + config.number("supervisor", "stop_timeout")
    for attempt in attempts:
        attempt.done.wait(max(0, deadline - time.monotonic()))


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
    """Run one whitelisted verb as the captain; `do` and `quiet` only open an authorization."""
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        raise CaptainError("[supervisor-params] argv must be a non-empty list of strings.")
    verb = argv[0]
    if verb in AUTHORIZED:
        args = do.parse(argv)
        row = do.request_authorization(supervisor.session.directory, args, "app", argv)
        return {"authorization": row}
    if verb not in COMMANDS:
        raise CaptainError(f"[command-refused] {verb!r} is not a whitelisted captain verb.")
    if verb == "dashboard" and argv[1:] != ["--once"]:
        raise CaptainError("[command-refused] dashboard runs only as `dashboard --once`.")
    reply = run_command(supervisor, argv, COMMANDS[verb])
    if verb in captain_bridge.NOTE_VERBS and reply["exit"] == 0:
        captain_bridge.add_note(supervisor.session.directory, argv)
    return reply


def typed(reply):
    """Add the outcome a client reads instead of parsing text: JSON `result` or a `refusal`."""
    if reply["exit"] == 0:
        with contextlib.suppress(ValueError):
            reply["result"] = json.loads(reply["stdout"])
        return reply
    message = reply["stderr"].strip().removeprefix("captain:").strip() or reply["stdout"].strip()
    match = PROTOCOL_CODE.search(message)
    reply["refusal"] = {"code": match[1] if match else "captain-error", "message": message}
    return reply


def run_command(supervisor, argv, timeout, extra_env=None):
    verb = argv[0]
    meta = supervisor.session.meta
    env = {key: value for key, value in os.environ.items() if key not in CREW_ENV}
    env.update(
        CAPTAIN_PROJECT=meta["project"],
        CAPTAIN_SESSION=meta["id"],
        **{supervision.TOKEN_ENV: supervisor.token},
        **(extra_env or {}),
    )
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
    literals = supervisor.literals
    (out, clipped), (err, err_clipped) = _clip(stdout, literals), _clip(stderr, literals)
    reply = {"exit": process.returncode, "stdout": out, "stderr": err}
    if clipped or err_clipped:
        reply["truncated"] = True
    return typed(reply)


def resolve_authorization(supervisor, params):
    """Approve or deny a request; an approved app request runs at once, a captain's on its rerun."""
    reason = params.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise CaptainError("[supervisor-params] reason must be a string.")
    directory = supervisor.session.directory
    row = do.resolve_authorization(
        directory, _text(params, "request"), _text(params, "decision"), reason
    )
    reply = {"request": row["id"], "state": row["state"]}
    if row["state"] == "approved" and row["origin"] == "app":
        extra = {do.AUTHORIZATION_ENV: row["id"]}
        reply["run"] = run_command(supervisor, row["argv"], AUTHORIZED[row["argv"][0]], extra)
        rows = store.read_json(directory / "protocol.json").get("authorizations", [])
        reply["state"] = next(r["state"] for r in rows if r["id"] == row["id"])
    return reply


def authorizations(supervisor, params):
    path = supervisor.session.directory / "protocol.json"
    rows = store.read_json(path).get("authorizations", []) if path.exists() else []
    return {"authorizations": rows}


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
    return eventlog.redact(page, supervisor.literals)


def step(supervisor, params):
    entry = transcripts.step(
        current_session(supervisor),
        _text(params, "agent"),
        _text(params, "id"),
        incarnation=params.get("incarnation"),
    )
    return eventlog.redact(entry, supervisor.literals)


def rename(supervisor, params):
    name = _text(params, "name")
    try:
        rename_session(supervisor.session.directory, name)
    except CaptainError as exc:
        raise CaptainError(f"[session-name] {exc}") from exc
    return {"session_id": supervisor.session.meta["id"], "name": name.strip()}


def captain_idle_at(supervisor):
    """When the captain's newest turn ended, from the tail of its own hook events; None if never."""
    path = supervisor.session.events("captain")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    start = max(0, size - CAPTAIN_TAIL)
    events, _ = eventlog.read_events(path, start, max_bytes=CAPTAIN_TAIL)
    for event in reversed(events):
        if event_status(event) in ("done", "idle") and isinstance(event.get("timestamp"), str):
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(event["timestamp"]).timestamp()
    return None


def questions(supervisor, params=None):
    """Every open supervised question with who it waits on, recording any change of route."""
    directory = supervisor.session.directory
    path = directory / "protocol.json"
    state = store.read_json(path) if path.exists() else {"active": {}, "assignments": {}}
    idle, rows, routes = captain_idle_at(supervisor), [], {}
    for crew_id, assignment_id in state["active"].items():
        assignment = state["assignments"].get(assignment_id) or {}
        question = assignment.get("question")
        if not question or not assignment.get("supervised"):
            continue
        target = protocol.waiting_on(question, idle)
        if question.get("waiting_on") != target:
            routes[(crew_id, question["id"])] = target
        rows.append(
            {
                "crew": crew_id,
                "assignment": assignment_id,
                "question": question["id"],
                "text": question["text"],
                "grant_request": question.get("grant_request"),
                "delivered_at": question.get("delivered_at"),
                "escalated": bool(question.get("escalated")),
                "waiting_on": target,
            }
        )
    if routes:
        protocol.route_questions(directory, routes)
    return {"questions": rows}


def route(supervisor):
    with contextlib.suppress(CaptainError, OSError, KeyError):
        questions(supervisor)  # best effort: the next tick routes again


def diff(supervisor, params):
    return protocol.assignment_diff(supervisor.session.directory, _text(params, "assignment"))


def mark_reviewed(supervisor, params):
    """Mark a report reviewed, or with `result` one isolated result of that assignment."""
    assignment_id, result_id = _text(params, "assignment"), params.get("result")
    note = params.get("note")
    if note is not None and not isinstance(note, str):
        raise CaptainError("[supervisor-params] note must be a string.")
    if result_id is not None:
        try:
            manifest = results.read(current_session(supervisor), _text(params, "result"))
        except (CaptainError, OSError) as exc:
            raise CaptainError(f"[review-result] Result {result_id} is unreadable: {exc}") from exc
        if manifest["assignment_id"] != assignment_id:
            raise CaptainError(f"[review-result] Result {result_id} is not {assignment_id}'s.")
    return protocol.mark_reviewed(supervisor.session.directory, assignment_id, result_id, note)


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
        message["params"] = eventlog.redact(params, supervisor.literals)
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
                route(supervisor)
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
    "resolve_authorization": resolve_authorization,
    "authorizations": authorizations,
    "questions": questions,
    "diff": diff,
    "mark_reviewed": mark_reviewed,
    "start_captain": start_captain,
    "captain_state": captain_state,
    "message_captain": message_captain,
    "interrupt_captain": interrupt_captain,
    "stop_captain": stop_captain,
    "answer_captain": answer_captain,
    "captain_requests": captain_requests,
    "providers": providers,
    "sign_in": sign_in,
    "cancel_sign_in": cancel_sign_in,
    "usage": agent_usage,
    "resolve_captain_delivery": resolve_captain_delivery,
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
        given = token.encode() if isinstance(token, str) else b""
        full = hmac.compare_digest(given, supervisor.token.encode())
        scoped = hmac.compare_digest(given, supervisor.captain_token.encode())
        if not (full or scoped):
            return {"error": refusal("[supervisor-token] Wrong or missing token.", TOKEN_ERROR)}
        if method not in METHODS:
            return {"error": refusal(f"[unknown-method] {method}", UNKNOWN_METHOD)}
        if scoped and method not in CAPTAIN_METHODS:
            message = (
                f"[supervisor-captain-token] {method} is the user's to call, not the captain's."
            )
            return {"error": refusal(message, TOKEN_ERROR)}
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
        for secret in supervisor.literals:
            eventlog.hide(secret)
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
                route(supervisor)
        finally:
            with supervisor.lock:
                supervisor.closing.set()
            server.shutdown()
            stop_captain(supervisor)
            stop_all(supervisor)
            cancel_sign_ins(supervisor)


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
