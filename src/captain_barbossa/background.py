"""Background crew: one read-only headless turn per assignment, run by a detached owner.

Claim and withdrawal are renames of one intent file, so exactly one wins without a lock a
stopped process could hold. The owner holds its lock for the whole run and writes the
outcome before exiting: a free lock with no outcome means the run was lost.

Lifetime owns native process-group shutdown and its evidence. The background owner
also requires output readers to finish before publishing that receipt; an escaped pipe
holder leaves cleanup pending. Cancellation is requested through a file.
"""

import fcntl
import json
import math
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, execution, lifetime, native_history, protocol
from .do import QUIET_PROMPT
from .models import headless_argv, headless_report
from .runtime import CaptainError
from .sessions import Session
from .store import lock, private_dir, read_json, state_root, temp_root, write_json, write_text

# Seconds `crew --background` waits for the owner's startup receipt.
STARTUP = 10
# Seconds a withdrawal waits for the protocol lock before leaving it to the next wait.
LOCK_WAIT = 2
# Seconds `interrupt` waits for the owner to confirm a cancellation.
CANCEL_WAIT = 10
POLL = 0.2
# Disk bounds per run. A turn's result is one JSON document or a JSON-lines stream that
# only parses whole, so output past the limit fails the run instead of being cut.
STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
# The report kept in protocol state and wait deliveries; the whole one stays on disk.
REPORT_LIMIT = 4000
TAIL = 1000
READ_ONLY = {"read", "search"}
FAILED = ("failed", "cancelled", "lost")
UNRESERVED = "[background-start] recruitment stopped before it was reserved"
# Providers whose read-only gate holds against the user's own configuration: codex
# ignores it under an OS read-only sandbox; pi drops extensions, MCP and skills by flag
# (models.headless_argv isolated=True). claude keeps user hooks and settings allow rules
# and grok offers no flag to drop its MCP servers and plugins, so both are refused.
SUPPORTED = ("codex", "pi")


def check_provider(provider):
    if provider not in SUPPORTED:
        raise CaptainError(
            f"[background-provider] {provider}'s headless turn still runs the user's own "
            "hooks, plugins or permission rules, so it cannot be held read-only unwatched. "
            f"Use {' or '.join(SUPPORTED)}, or quiet or a visual crew."
        )


def timeout():
    value = config.number("crew", "background_timeout")
    if not math.isfinite(value):
        raise CaptainError(f"[crew] background_timeout must be finite seconds. Got {value!r}.")
    return value


def refuse_flags(args):
    """Refuse what a read-only paneless turn cannot honour, before anything is reserved."""
    timeout()
    if args.crew_agent:
        check_provider(args.crew_agent)
    placed = [
        flag
        for flag, value in (
            ("--placement", args.placement),
            ("--direction", args.direction),
            ("--split-pane", args.split_pane),
            ("--owns", args.owns),
            ("--handoff", args.handoff),
        )
        if value
    ]
    if placed:
        raise CaptainError(f"[background-flags] --background takes no {', '.join(placed)}.")
    if set(args.allow) - READ_ONLY:
        raise CaptainError(
            "[background-read-only] Background crew are read-only in this version; "
            "--allow takes only read and search. Recruit a visual crew to edit."
        )


def stem(crew):
    return crew.session.directory / "background" / f"{crew.crew_id}-{crew.record['incarnation_id']}"


def paths(base):
    return {
        kind: base.with_name(f"{base.name}{suffix}")
        for kind, suffix in (
            ("record", ".json"),
            ("lock", ".lock"),
            ("cancel", ".cancel"),
            ("stdout", ".out"),
            ("stderr", ".err"),
            ("report", ".report.md"),
            ("log", ".owner.log"),
            ("launch", ".launch"),
            ("intent", ".intent"),
            ("claimed", ".claimed"),
            ("abandoned", ".abandoned"),
        )
    }


@contextmanager
def held(path):
    """Yield True when this process took the lock, False when someone else holds it."""
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


@contextmanager
def launching(crew):
    """The recruiter's lock until claim or withdrawal, so `alive` can tell a recruiter still
    deciding from one that died before any owner took over."""
    base = stem(crew)
    private_dir(base.parent)
    with lock(paths(base)["launch"]):
        yield


def alive(crew):
    """Whether a recruiter or an owner still holds this crew's run."""
    files = paths(stem(crew))
    try:
        record = read_json(files["record"])
    except CaptainError:
        # Preparation publishes the assignment before writing the run record.
        if files["launch"].exists():
            with held(files["launch"]) as free:
                return not free
        return False
    if record["state"] not in ("launching", "running"):
        return False
    for name in ("launch", "lock") if record["state"] == "launching" else ("lock",):
        with held(files[name]) as free:
            if not free:
                return True
    # A launch nobody holds was left by a dead recruiter; a late owner's claim finds its
    # assignment closed and withdraws.
    return False


def close(assignment, outcome, summary):
    """End an assignment that produced no report: visibly an error, never a done."""
    protocol.notice(assignment, "error", summary)
    assignment.update(state="done", outcome=outcome, report=summary)


def reconcile(crew, assignment):
    """Close an unfinished assignment whose owner is gone; the caller holds the checkpoint."""
    if assignment["state"] == "done" or alive(crew):
        return
    if paths(stem(crew))["abandoned"].exists():
        close(assignment, "failed", "[background-start] The run was withdrawn before it started.")
        return
    close(
        assignment,
        "lost",
        "[background-lost] The owner process ended without recording an outcome; "
        f"nothing was restarted. Evidence: {stem(crew).parent}",
    )


def describe(crew, assignment):
    """(state, source, detail) for status, from the assignment first, then the owner."""
    if not assignment:
        return "failed", "process", UNRESERVED
    if assignment.get("outcome") in FAILED:
        return "failed", "process", assignment["report"]
    if assignment["state"] == "done":
        return "done", "mail", assignment.get("report") or "assignment completed"
    if alive(crew):
        return "working", "process", "background owner running"
    # A read path records nothing; the next wait closes it as lost and delivers that.
    return "failed", "process", "[background-lost] owner gone without an outcome"


def prepare(crew, project, provider, binary, model, task, assignment_id):
    """Queue the task and record launch intent before any process exists."""
    directory = crew.session.directory
    files = paths(stem(crew))
    message_id = protocol.deliver(
        crew, task, assignment_id=assignment_id, initial=True, queue_only=True
    )
    environment = {
        "CAPTAIN_ROLE": "crew",
        "CAPTAIN_CREW": crew.crew_id,
        "CAPTAIN_INCARNATION": crew.record["incarnation_id"],
        "CAPTAIN_ASSIGNMENT": assignment_id,
        "CAPTAIN_SESSION": directory.name,
        "CAPTAIN_PROJECT": str(Path(project).resolve()),
        "CAPTAIN_STATE_ROOT": str(state_root()),
        "CAPTAIN_TEMP_ROOT": str(temp_root()),
    }
    execution_root = execution.root(crew.session, crew.record)
    environment["CAPTAIN_WORKTREE_ID"] = crew.record.get("worktree_id", "")
    history = native_history.prepare(
        crew, incarnation=crew.record["incarnation_id"], project=project, background=True
    )
    environment.update(history["env"])
    argv = headless_argv(provider, binary, f"{QUIET_PROMPT}\n\n{task}", model=model, isolated=True)
    argv[-1:-1] = history["argv"]
    ref = lifetime.prepare(crew.session, crew.record, argv, environment, execution_root)
    crew.record["lifetime_id"] = ref
    write_json(crew.session.meta_path, crew.session.meta)
    record = {
        "state": "launching",
        "lifetime_id": ref,
        "at": time.time(),
        "session": str(directory),
        "crew": crew.crew_id,
        "assignment_id": assignment_id,
        "message_id": message_id,
        "provider": provider,
        "model": model,
        "cwd": str(execution_root),
        "timeout": timeout(),
    }
    write_json(files["record"], record)
    write_text(files["intent"], assignment_id)
    return files


def start(files):
    """Run the owner; return once it claims the run, else withdraw the intent and raise.

    The deadline is hard: an owner stopped anywhere, even holding the protocol lock, cannot
    hold recruitment open past STARTUP plus LOCK_WAIT, nor start the run later.
    """
    record = read_json(files["record"])
    try:
        with files["log"].open("ab") as log:
            os.chmod(files["log"], 0o600)
            owner = subprocess.Popen(
                [sys.executable, "-m", "captain_barbossa.background", str(files["record"])],
                cwd=record["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
    except OSError as exc:
        abandon(files, f"could not start the owner: {exc}")
        raise CaptainError(f"[background-start] Could not start the owner: {exc}") from exc
    deadline = time.monotonic() + STARTUP
    while time.monotonic() < deadline and owner.poll() is None:
        if files["claimed"].exists():
            return
        time.sleep(0.05)
    reason = f"the owner did not claim the run within {STARTUP}s; see {files['log']}"
    if abandon(files, reason):
        raise CaptainError(f"[background-start] {reason[0].upper()}{reason[1:]}.")


def abandon(files, reason):
    """Withdraw intent no owner has claimed and close its assignment; True when withdrawn.
    If the protocol lock stays held, the next wait's reconcile closes it instead."""
    try:
        os.rename(files["intent"], files["abandoned"])
    except FileNotFoundError:
        return False  # the owner claimed it first
    record = {**read_json(files["record"]), "state": "abandoned", "at": time.time()}
    write_json(files["record"], record)
    try:
        with protocol.checkpoint(Path(record["session"]), timeout=LOCK_WAIT) as state:
            assignment = state["assignments"].get(record["assignment_id"])
            if assignment and assignment["state"] != "done":
                close(assignment, "failed", f"[background-start] {reason}")
    except CaptainError:
        pass
    return True


def cancel(crew, reason):
    """Ask the owner to stop its run; report what actually happened, signalling nothing."""
    files = paths(stem(crew))
    if not alive(crew):
        return "not running; nothing was signalled"
    write_text(files["cancel"], reason or "captain")
    deadline = time.monotonic() + CANCEL_WAIT
    while time.monotonic() < deadline:
        record = read_json(files["record"])
        if record["state"] == "finished":
            if record["outcome"] == "cancelled":
                return "cancelled"
            return f"already finished as {record['outcome']}; the cancel did not take effect"
        if record["state"] == "abandoned":
            return "withdrawn before it started"
        time.sleep(POLL)
    return "cancel requested; the owner has not confirmed it yet"


def _copy(stream, path, limit, over, failed):
    """Keep bounded output, reporting read/write errors before any lifetime receipt."""
    try:
        with (
            stream,
            os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out,
        ):
            written = 0
            for chunk in iter(lambda: stream.read1(65536), b""):
                room = max(limit - written, 0)
                if len(chunk) > room:
                    over.set()
                out.write(chunk[:room])
                written += min(len(chunk), room)
    except (OSError, ValueError):
        failed.set()


def _tail(path):
    try:
        return path.read_bytes()[-TAIL:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _bounded(text, report):
    if len(text) <= REPORT_LIMIT:
        return text
    return f"{text[:REPORT_LIMIT]}\n(truncated; full report: {report})"


def _settle(record, outcome, text):
    with protocol.checkpoint(Path(record["session"])) as state:
        assignment = state["assignments"].get(record["assignment_id"])
        if not assignment or assignment["state"] == "done":
            return
        if outcome == "done":
            assignment.update(state="done", report=text)
            protocol.notice(assignment, "done", text)
        else:
            close(assignment, outcome, text)


def _claim(files):
    """Take the run by renaming its intent, racing `abandon`; None if not ours. Intent that
    expired, or whose assignment a wait already closed, is withdrawn instead of run."""
    try:
        os.rename(files["intent"], files["claimed"])
    except FileNotFoundError:
        return None
    record = read_json(files["record"])
    session = Path(record["session"])
    with protocol.checkpoint(session) as state:
        assignment = state["assignments"].get(record["assignment_id"])
        # Expiry of stale intent, not liveness (the flocks decide that): a recruiter that
        # died before withdrawing must not have its run start arbitrarily late.
        late = time.time() - record["at"] >= 2 * STARTUP
        if late or not assignment or assignment["state"] != "working":
            os.rename(files["claimed"], files["abandoned"])
            write_json(files["record"], {**record, "state": "abandoned", "at": time.time()})
            return None
        record = {**record, "state": "running", "owner": os.getpid()}
        write_json(files["record"], record)
    protocol.receipt(session, record["crew"], [record["message_id"]])
    return record


def run(path):
    """The owner: hold the lock for the whole run and record exactly one outcome."""
    record_path = Path(path)
    files = paths(record_path.with_name(record_path.name.removesuffix(".json")))
    with held(files["lock"]) as ours:
        if not ours:
            return  # another owner holds this run
        record = _claim(files)
        if record is None:
            return
        try:
            outcome, text = _execute(files, record)
        except Exception as exc:
            outcome, text = "failed", f"[background-failed] owner error: {exc!r}"
        if outcome == "done":
            write_text(files["report"], text)
            text = _bounded(text, files["report"])
        _settle(record, outcome, text)
        write_json(
            record_path,
            {**read_json(record_path), "state": "finished", "outcome": outcome, "at": time.time()},
        )


def _execute(files, record):
    if not record.get("lifetime_id"):
        return (
            "failed",
            "[background-lifetime] Unmanaged legacy launch retained; recruit a new crew.",
        )
    directory = Path(record["session"])
    current = Session(directory, read_json(directory / "session.json"))
    with lifetime.claim(current, record["lifetime_id"]) as job:
        stopped = None
        if files["cancel"].exists():
            job.cancel_before_start()
            return "cancelled", f"[background-cancelled] {files['cancel'].read_text().strip()}"
        reader_timeout = config.number("lifetime", "shutdown_timeout", below=float("inf"))
        deadline = time.monotonic() + record["timeout"]
        job.start(
            detached=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        over, failed = threading.Event(), threading.Event()
        readers = []
        try:
            for stream, path, limit in (
                (job.stdout, files["stdout"], STDOUT_LIMIT),
                (job.stderr, files["stderr"], STDERR_LIMIT),
            ):
                reader = threading.Thread(
                    target=_copy, args=(stream, path, limit, over, failed), daemon=True
                )
                reader.start()
                readers.append(reader)
            while job.poll() is None:
                if files["cancel"].exists():
                    stopped = "cancelled"
                elif time.monotonic() >= deadline:
                    stopped = "timeout"
                elif over.is_set():
                    stopped = "output"
                elif failed.is_set():
                    stopped = "reader"
                if stopped:
                    break
                time.sleep(POLL)
        finally:
            try:
                verdict = job.shutdown()
            finally:
                # An escaped pipe holder must not hang the owner or authorize cleanup.
                reader_deadline = time.monotonic() + reader_timeout
                for reader in readers:
                    reader.join(timeout=max(0, reader_deadline - time.monotonic()))
                if (
                    len(readers) != 2
                    or any(reader.is_alive() for reader in readers)
                    or failed.is_set()
                ):
                    job.invalidate("background output readers did not complete safely")
                    stopped = "reader"
        if stopped == "reader":
            return (
                "failed",
                "[background-output] Output readers failed or remain active; artifacts retained.",
            )
        if not verdict.ok:
            return "failed", f"[background-shutdown] {verdict.reason}"
        verdict = job.finish()
        if not verdict.ok:
            return "failed", f"[background-shutdown] {verdict.reason}"
        exit_code = job.native_returncode
        if stopped == "cancelled":
            return "cancelled", f"[background-cancelled] {files['cancel'].read_text().strip()}"
        if stopped == "timeout":
            return "failed", f"[background-timeout] stopped after {record['timeout']}s."
        if stopped == "output" or over.is_set():
            return "failed", (
                f"[background-output-limit] output passed {STDOUT_LIMIT} bytes (or stderr "
                f"{STDERR_LIMIT}); partial output: {files['stdout']}"
            )
        stdout = files["stdout"].read_bytes().decode("utf-8", "replace")
        try:
            text, _ = headless_report(record["provider"], stdout, record["model"])
        except CaptainError as exc:
            detail = _tail(files["stderr"])
            return "failed", f"[background-failed] exit {exit_code}: {exc}" + (
                f"\nstderr: {detail}" if detail else ""
            )
        return "done", text


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except Exception as exc:  # the log is the only witness an unwatched owner has
        print(json.dumps({"error": repr(exc), "at": time.time()}), file=sys.stderr)
        raise
