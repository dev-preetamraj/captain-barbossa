"""Cooperative native-child receipts, not containment of arbitrary descendants.

The native CLI is the guard's own child, so its reaped exit is authoritative. Escaped
descendants are evidence only for worktree removal: Linux proves none survive through a
subreaper owner; macOS scans descendants per poll, which can miss a fast double fork.
"""

import ctypes
import errno
import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config, runtime, sessions, store
from .runtime import CaptainError


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise CaptainError("Invalid lifetime reference.")
    return value


def _plain(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise CaptainError("Lifetime paths must not contain symlinks.")
    return path


def _session(session):
    directory = _plain(session.directory)
    meta = store.read_json(_plain(directory / "session.json"), max_bytes=1024 * 1024)
    if not isinstance(meta, dict):
        raise CaptainError("Invalid lifetime session metadata.")
    project = _plain(meta["project"])
    if (
        meta["id"] != _id(directory.name)
        or meta["id"] != session.meta["id"]
        or meta["project"] != session.meta["project"]
        or sessions.locate(project, meta["id"]) != directory
        or not sessions.is_durable(directory, project)
        or directory.is_relative_to(project)
    ):
        raise CaptainError("Lifetime requires the authoritative durable session.")
    return sessions.Session(directory, meta)


def _path(session, ref, suffix=".json"):
    return _plain(session.directory / "lifetimes" / f"{_id(ref)}{suffix}")


def _write(session, ref, record):
    path = _path(session, ref)
    store.write_json(path, record)
    for parent in (path.parent, session.directory):
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read(session, ref):
    """Read durable evidence; caller-supplied dictionaries are never evidence."""
    session = _session(session)
    record = store.read_json(_path(session, ref), max_bytes=1024 * 1024)
    if (
        not isinstance(record, dict)
        or record.get("id") != _id(ref)
        or record.get("incarnation_id") != _id(ref)
        or record.get("session_id") != session.meta["id"]
        or record.get("project") != session.meta["project"]
        or not isinstance(record.get("crew_id"), str)
        or not record.get("crew_id")
        or record.get("crew_id", "").casefold() == "captain"
        or record.get("state") not in {"prepared", "claimed", "exited"}
        or type(record.get("revoked")) is not bool
        or (
            record["state"] == "exited"
            and record.get("receipt_version") != 2
            and type(record.get("returncode")) is not int
        )
    ):
        raise CaptainError("Lifetime identity or receipt mismatch.")
    _plain(record["root"])
    return record


def revoked(session, crew_record):
    ref = crew_record.get("lifetime_id")
    return bool(ref and read(session, ref)["revoked"])


def prepare(session, crew_record, argv, env, cwd):
    """Reserve one launch. env contains explicit nonsecret overrides, never os.environ."""
    session = _session(session)
    ref = _id(crew_record["incarnation_id"])
    crew_id = crew_record["id"]
    if not isinstance(crew_id, str) or not crew_id or crew_id.casefold() == "captain":
        raise CaptainError("Lifetime requires a crew identity.")
    if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise CaptainError("Lifetime requires a native command.")
    # Native-home roots native_history passes through; values are paths, never credentials.
    allowed = {
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
        "GROK_HOME",
        "HOME",
        "XDG_CONFIG_HOME",
    }
    if any(
        not isinstance(key, str)
        or (key not in allowed and not key.startswith("CAPTAIN_"))
        or re.search(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|KEY", key, re.I)
        or not isinstance(value, str)
        or "\0" in key + value
        or "=" in key
        for key, value in env.items()
    ):
        raise CaptainError("Lifetime environment must contain nonsecret launch overrides only.")
    root = str(_plain(cwd))
    if root != str(_plain(crew_record.get("execution_root") or session.meta["project"])):
        raise CaptainError("Lifetime execution root mismatch.")
    worktree = crew_record.get("worktree_id")
    if worktree is not None and _id(worktree) != ref:
        raise CaptainError("Lifetime worktree identity mismatch.")
    store.private_dir(_path(session, ref).parent)
    with (
        store.lock(_path(session, ref, ".owner"), timeout=0),
        store.lock(_path(session, ref, ".lock")),
    ):
        if _path(session, ref).exists():
            raise CaptainError("Lifetime was already prepared.")
        _write(
            session,
            ref,
            {
                "id": ref,
                "session_id": session.meta["id"],
                "crew_id": crew_id,
                "incarnation_id": ref,
                "project": session.meta["project"],
                "root": root,
                "worktree_id": worktree,
                "argv": list(argv),
                "env": dict(env),
                "state": "prepared",
                "revoked": False,
                "pane": None,
                "close_state": "not-requested",
            },
        )
    return ref


def bind(session, ref, crew_record):
    """Bind the exact terminal once, after Herdr has allocated it."""
    session = _session(session)
    with store.lock(_path(session, ref, ".lock")):
        record = read(session, ref)
        roster = session.meta["crew"].get(record["crew_id"], {})
        if (
            roster.get("incarnation_id") != record["incarnation_id"]
            or crew_record.get("incarnation_id") != record["incarnation_id"]
            or crew_record.get("id") != record["crew_id"]
            or record["revoked"]
        ):
            raise CaptainError("Cannot bind a stale or revoked lifetime.")
        pane = {
            "workspace_id": session.meta["workspace"],
            "tab_id": crew_record.get("tab"),
            "pane_id": crew_record.get("pane"),
            "terminal_id": crew_record.get("terminal_id"),
        }
        if not all(isinstance(value, str) and value for value in pane.values()):
            raise CaptainError("Lifetime binding requires exact pane and terminal IDs.")
        if record["pane"] is not None and record["pane"] != pane:
            raise CaptainError("Lifetime terminal cannot be rebound.")
        record["pane"] = pane
        _write(session, ref, record)


def _duration(name):
    return config.number("lifetime", name)


_LINUX = sys.platform.startswith("linux")
_DARWIN = sys.platform == "darwin"
_PR_SET_CHILD_SUBREAPER = 36
_PROC = Path("/proc")
# Absolute: the Git environment's PATH does not include it.
_LSOF = "/usr/sbin/lsof"


def _subreaper(enabled):
    """Linux only: orphaned descendants reparent to the owner instead of init."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def _pids():
    """Linux: /proc process entries, listed without stat-ing them.

    Python 3.11's Path.glob stats each match, silently dropping dangling or unreadable
    links such as a deleted cwd; a listing failure raises instead of reading as empty.
    """
    return [entry for entry in _PROC.iterdir() if entry.name.isdigit()]


def _children(pid):
    """Linux: {child: (state, process group)} for direct children of pid, from /proc."""
    children = {}
    for entry in _pids():
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue  # exited during the scan
        if int(fields[1]) == pid:
            children[int(entry.name)] = (fields[0], int(fields[2]))
    return children


def _live_children(pid):
    """Linux: live (non-zombie) direct children of pid."""
    return {child for child, (state, _) in _children(pid).items() if state != "Z"}


def _reap_group_zombies(pgid):
    """Linux: reap killed group members subreaped to this owner.

    An unreaped zombie still answers killpg(pgid, 0), so group absence could never be
    confirmed. Only exact zombie children in pgid are reaped; live members stay visible.
    Best effort: a failed scan reaps nothing, and the absence check still decides.
    """
    try:
        children = _children(os.getpid())
    except (OSError, ValueError):
        return
    for child, (state, group) in children.items():
        if state == "Z" and group == pgid:
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass


def cwd_holders(root):
    """Live processes whose working directory is inside root; never signals them.

    A sweep that cannot see every same-user process raises, so the caller retains.
    """
    root = Path(root).resolve()
    holders = set()
    if _LINUX:
        try:
            links = [entry / "cwd" for entry in _pids()]
        except OSError as exc:
            raise CaptainError(f"[cleanup-pending] cwd sweep failed: {exc}") from exc
        for link in links:
            try:
                cwd = os.readlink(link)
            except FileNotFoundError:
                continue  # exited during the sweep
            except OSError as exc:
                try:
                    foreign = link.parent.stat().st_uid != os.getuid()
                except FileNotFoundError:
                    continue
                if not foreign:
                    raise CaptainError(f"[cleanup-pending] cwd sweep failed: {exc}") from exc
                continue
            if Path(cwd.removesuffix(" (deleted)")).is_relative_to(root):
                holders.add(int(link.parent.name))
    else:
        try:
            result = subprocess.run(
                [_LSOF, "-w", "-n", "-P", "-d", "cwd", "-F", "pn"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptainError(f"[cleanup-pending] cwd sweep unavailable: {exc}") from exc
        if result.returncode or not result.stdout:
            raise CaptainError("[cleanup-pending] cwd sweep failed; retaining worktree.")
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid is not None and Path(line[1:]).is_relative_to(root):
                holders.add(pid)
    holders.discard(os.getpid())
    return sorted(holders)


class _BsdInfo(ctypes.Structure):
    _fields_ = [
        *((name, ctypes.c_uint32) for name in ("flags", "status", "xstatus", "pid", "ppid")),
        *((name, ctypes.c_uint32) for name in ("uid", "gid", "ruid", "rgid", "svuid")),
        ("svgid", ctypes.c_uint32),
        ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        *((name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


class _DescendantScan:
    """Darwin best-effort escape detection; never proof that nothing escaped."""

    def __init__(self):
        self._libproc = ctypes.CDLL(None, use_errno=True)
        self._seen = set()

    def _children(self, pid):
        size = 256
        while True:
            buffer = (ctypes.c_int * size)()
            count = self._libproc.proc_listchildpids(pid, buffer, ctypes.sizeof(buffer))
            if count < 0:
                raise OSError(ctypes.get_errno(), "proc_listchildpids failed")
            if count < size:
                return [child for child in buffer[:count] if child > 0]
            size *= 4

    def _group(self, pid):
        info = _BsdInfo()
        if self._libproc.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        ) != ctypes.sizeof(info):
            return None  # gone or a zombie: no longer able to write
        return info.pgid

    def escaped(self):
        """True when a live descendant seen now or earlier has left the guard group."""
        pending, current = [os.getpid()], set()
        while pending:
            for child in self._children(pending.pop()):
                if child not in current:
                    current.add(child)
                    pending.append(child)
        self._seen |= current
        groups = {pid: self._group(pid) for pid in self._seen}
        # Forget the dead, so a recycled PID is never mistaken for an old descendant.
        self._seen = {pid for pid, group in groups.items() if group is not None}
        return any(group not in (None, os.getpgrp()) for group in groups.values())


def _foreground(fd):
    """The terminal's foreground group, or None for a terminal that is not controlling.

    Without a controlling terminal there is no foreground or job-control signal routing
    to own, so only the attributes need restoring.
    """
    try:
        return os.tcgetpgrp(fd)
    except OSError as exc:
        if exc.errno == errno.ENOTTY and os.isatty(fd):
            return None
        raise


@contextmanager
def _terminal_access():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class ManagedJob:
    """A claim-bound live guard; disk PIDs and caller assertions never grant a receipt."""

    def __init__(self, session, ref):
        self.session, self.ref = session, ref
        self._active, self._owner = True, os.getpid()
        self._guard = None
        self._control = self._status = None
        self._buffer = b""
        self._ready = self._started = self._reaped = False
        self.native_returncode = None
        self._escape = None
        self._baseline = self._survivors = None
        self._shutdown = None
        self._failure = None
        self._terminal = None
        self.stdout = self.stderr = None

    def _owned(self):
        if not self._active or self._owner != os.getpid():
            raise CaptainError("Inactive managed lifetime owner.")

    def invalidate(self, reason):
        self._owned()
        self._failure = self._failure or str(reason)

    def _drain(self, *, stopping=False):
        if self._status is None:
            raise CaptainError("Managed guard has not started.")
        try:
            while True:
                try:
                    data = os.read(self._status, 4096)
                except BlockingIOError:
                    break
                if not data:
                    if not stopping:
                        raise CaptainError("Managed guard control was lost.")
                    break
                self._buffer += data
                if len(self._buffer) > 65536:
                    raise CaptainError("Managed guard control exceeded its bound.")
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise CaptainError("Malformed managed guard message.")
                    if message == {"type": "ready"} and not self._ready:
                        self._ready = True
                    elif (
                        message.get("type") == "exit"
                        and set(message) == {"type", "returncode"}
                        and type(message["returncode"]) is int
                        and self.native_returncode is None
                        and self._started
                    ):
                        self.native_returncode = message["returncode"]
                    elif (
                        message.get("type") == "escape"
                        and set(message) == {"type", "evidence"}
                        and message["evidence"] in {"observed", "unverified"}
                        and self._started
                    ):
                        if self._escape != "observed":
                            self._escape = message["evidence"]
                    else:
                        raise CaptainError("Managed guard reported invalid or escaped execution.")
            if stopping and self._buffer:
                raise CaptainError("Truncated managed guard message.")
        except (OSError, ValueError, CaptainError) as exc:
            self.invalidate(str(exc))
            raise CaptainError(f"Managed guard control failed: {exc}") from exc

    def start(self, *, detached=False, stdin=None, stdout=None, stderr=None):
        self._owned()
        if self._guard is not None or self._failure:
            raise CaptainError("Managed launch cannot be replayed.")
        record = read(self.session, self.ref)
        if record["revoked"]:
            raise CaptainError("Managed launch was revoked.")
        if not detached:
            if any(stream is not None for stream in (stdin, stdout, stderr)):
                raise CaptainError("Interactive managed jobs inherit terminal streams directly.")
            if not os.isatty(0):
                raise CaptainError("Managed launch requires a terminal.")
            foreground = _foreground(0)
            if foreground not in (None, os.getpgrp()):
                raise CaptainError("Managed launch requires ownership of the foreground terminal.")
            self._terminal = (foreground, termios.tcgetattr(0))
        if _LINUX:
            try:
                _subreaper(True)
                self._baseline = _live_children(os.getpid())
            except (OSError, ValueError, AttributeError):
                self._baseline = None
        control_read, control_write = os.pipe()
        status_read, status_write = os.pipe()
        self._control, self._status = control_write, status_read
        os.set_blocking(status_read, False)
        try:
            self._guard = subprocess.Popen(
                [
                    runtime.executable(sys.executable),
                    "-m",
                    "captain_barbossa.lifetime",
                    str(self.session.directory),
                    _id(self.ref),
                    str(control_read),
                    str(status_write),
                    "detached"
                    if detached
                    else "terminal"
                    if self._terminal[0] is not None
                    else "uncontrolled",
                ],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=record["root"],
                pass_fds=(control_read, status_write),
                start_new_session=detached,
                process_group=None if detached else 0,
            )
            self.stdout, self.stderr = self._guard.stdout, self._guard.stderr
        except OSError as exc:
            self.invalidate(str(exc))
            raise CaptainError(f"Cannot start managed guard: {exc}") from exc
        finally:
            os.close(control_read)
            os.close(status_write)
        deadline = time.monotonic() + _duration("startup_timeout")
        while not self._ready:
            self._drain()
            if time.monotonic() >= deadline:
                self.invalidate("guard-startup-timeout")
                raise CaptainError("Managed guard did not become ready before its deadline.")
            if not self._ready:
                time.sleep(_duration("poll_interval"))
        if not detached and self._terminal[0] is not None:
            with _terminal_access():
                if os.tcgetpgrp(0) != self._terminal[0]:
                    self.invalidate("foreground-changed")
                    raise CaptainError("Foreground terminal changed before managed launch.")
                os.tcsetpgrp(0, self._guard.pid)
                if os.tcgetpgrp(0) != self._guard.pid:
                    self.invalidate("foreground-unconfirmed")
                    raise CaptainError("Managed foreground ownership could not be confirmed.")
        self._started = True
        try:
            os.write(self._control, b"GO\n")
        except OSError as exc:
            self.invalidate("guard-go-failed")
            raise CaptainError("Managed guard launch handshake failed.") from exc

    def poll(self):
        self._owned()
        if self._shutdown is None:
            self._drain()
        if self._failure:
            raise CaptainError(f"Managed lifetime retained: {self._failure}")
        return self.native_returncode

    def _signal(self, signum):
        if self._reaped or self._guard is None:
            raise CaptainError("Cannot signal a reaped or missing guard group.")
        # The unreaped child pins this group number; no identity is recovered from disk.
        try:
            os.killpg(self._guard.pid, signum)
        except ProcessLookupError:
            self.invalidate("guard-group-lost-before-reap")

    def _restore_terminal(self):
        if self._terminal is None:
            return
        foreground, attributes = self._terminal
        try:
            with _terminal_access():
                termios.tcsetattr(0, termios.TCSANOW, attributes)
                if (
                    foreground is not None
                    and self._guard is not None
                    and os.tcgetpgrp(0) == self._guard.pid
                ):
                    os.tcsetpgrp(0, foreground)
        except (OSError, termios.error) as exc:
            self.invalidate(f"terminal-restore-failed: {exc}")
        finally:
            self._terminal = None

    def shutdown(self):
        self._owned()
        if self._shutdown is not None:
            return self._shutdown
        if self._guard is None:
            self._restore_terminal()
            self._shutdown = Verdict(False, "guard-not-started")
            return self._shutdown
        try:
            self._signal(signal.SIGTERM)
            self._signal(signal.SIGCONT)
            deadline = time.monotonic() + _duration("terminate_timeout")
            while time.monotonic() < deadline:
                try:
                    self._drain()
                except CaptainError:
                    break
                time.sleep(_duration("poll_interval"))
            self._signal(signal.SIGKILL)
            # Restore immediately after the last real signal, before any wait for absence.
            self._restore_terminal()
            deadline = time.monotonic() + _duration("shutdown_timeout")
            guard_returncode = self._guard.wait(timeout=max(0, deadline - time.monotonic()))
            self._reaped = True
            if guard_returncode != -signal.SIGKILL:
                self.invalidate("guard-exited-before-final-kill")
            self._drain(stopping=True)
            while True:
                if _LINUX:
                    _reap_group_zombies(self._guard.pid)
                try:
                    os.killpg(self._guard.pid, 0)
                except ProcessLookupError:
                    self._shutdown = Verdict(not self._failure, self._failure or "group-absent")
                    self._count_survivors()
                    break
                if time.monotonic() >= deadline:
                    self.invalidate("managed-group-absence-unconfirmed")
                    break
                time.sleep(_duration("poll_interval"))
        except (OSError, subprocess.TimeoutExpired, CaptainError) as exc:
            self.invalidate(f"managed-shutdown-failed: {exc}")
        finally:
            if self._baseline is not None:
                try:
                    _subreaper(False)
                except OSError:
                    pass
            self._restore_terminal()
            if self._control is not None:
                os.close(self._control)
                self._control = None
            if self._status is not None:
                os.close(self._status)
                self._status = None
        self._shutdown = self._shutdown or Verdict(False, self._failure or "shutdown-unconfirmed")
        return self._shutdown

    def _count_survivors(self):
        """Linux: subreaped orphans of the guard are now direct children of this owner."""
        if self._baseline is None:
            return
        try:
            self._survivors = _live_children(os.getpid()) - self._baseline
        except (OSError, ValueError):
            self._survivors = None

    def _escape_state(self):
        if _LINUX:
            if self._baseline is None or self._survivors is None:
                return "subreaper", "unverified"
            return "subreaper", "survived" if self._survivors else "none-surviving"
        if _DARWIN:
            return "descendant-scan", {None: "none-observed"}.get(self._escape, self._escape)
        return "none", "unverified"

    def finish(self):
        self._owned()
        if self._failure or self._shutdown is None or not self._shutdown.ok or not self._reaped:
            return Verdict(False, self._failure or "shutdown-unconfirmed")
        evidence, escape = self._escape_state()
        with store.lock(_path(self.session, self.ref, ".lock")):
            record = read(self.session, self.ref)
            if record["state"] != "claimed":
                raise CaptainError("Managed shutdown receipt already finalized.")
            record.update(
                state="exited",
                receipt_version=2,
                native_returncode=self.native_returncode,
                shutdown={
                    "scope": "managed-process-group",
                    "state": "complete",
                    "guard_reaped": True,
                    "group_absent": True,
                    "escape_evidence": evidence,
                    "escape_state": escape,
                },
            )
            _write(self.session, self.ref, record)
        return Verdict(True, "managed-group-stopped")


@contextmanager
def claim(session, ref):
    """The actual native owner holds this through wait and guard shutdown."""
    session = _session(session)
    with store.lock(_path(session, ref, ".owner"), timeout=0):
        with store.lock(_path(session, ref, ".lock")):
            record = read(session, ref)
            current = _session(session).meta["crew"].get(record["crew_id"], {})
            if current.get("incarnation_id") != record["incarnation_id"]:
                raise CaptainError("Lifetime launch belongs to a stale incarnation.")
            if record["revoked"] or record["state"] != "prepared":
                raise CaptainError("Lifetime launch was revoked or already claimed.")
            record["state"] = "claimed"
            _write(session, ref, record)
        job = ManagedJob(session, ref)
        try:
            yield job
        finally:
            if job._guard is not None and job._shutdown is None:
                job.shutdown()
            for fd in (job._control, job._status):
                if fd is not None:
                    os.close(fd)
            job._active = False


def run(session_directory, ref):
    """Use the existing terminal/session with a live, separately owned foreground group."""
    directory = Path(session_directory)
    session = sessions.Session(directory, store.read_json(directory / "session.json"))
    with claim(session, ref) as job:
        previous = {}
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, stop)
            job.start()
            while job.poll() is None and not stopping and not read(session, ref)["revoked"]:
                time.sleep(_duration("poll_interval"))
            job.shutdown()
            verdict = job.finish()
            if not verdict.ok:
                raise CaptainError(f"Managed lifetime retained: {verdict.reason}")
            return job.native_returncode if job.native_returncode is not None else 1
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def revoke(session, ref):
    """Irreversible, including when launch or terminal closure failed."""
    session = _session(session)
    with store.lock(_path(session, ref, ".lock")):
        record = read(session, ref)
        record["revoked"] = True
        _write(session, ref, record)


@contextmanager
def _observe(path):
    """A missing or busy lock is uncertainty, never permission to create evidence."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


# Escape states that allow worktree removal: Linux proof, or a Darwin scan that saw none.
_NO_ESCAPE = {("subreaper", "none-surviving"), ("descendant-scan", "none-observed")}


def _shutdown_verdict(record, scope):
    if record["state"] == "prepared":
        return Verdict(True, "never-launched")
    if record["state"] != "exited":
        return Verdict(False, "exit-unknown")
    shutdown = record.get("shutdown")
    if (
        record.get("receipt_version") != 2
        or not isinstance(shutdown, dict)
        or shutdown.get("scope") != "managed-process-group"
        or shutdown.get("state") != "complete"
        or shutdown.get("guard_reaped") is not True
        or shutdown.get("group_absent") is not True
        or "native_returncode" not in record
        or (
            record["native_returncode"] is not None and type(record["native_returncode"]) is not int
        )
    ):
        return Verdict(False, "managed-shutdown-unconfirmed")
    if scope == "group":
        return Verdict(True, "managed-group-stopped")
    if scope == "native":
        # Only the native process writes its history; the guard reaped it, escapes aside.
        if type(record["native_returncode"]) is not int:
            return Verdict(False, "native-exit-unreported")
        return Verdict(True, "native-process-reaped")
    if (shutdown.get("escape_evidence"), shutdown.get("escape_state")) not in _NO_ESCAPE:
        return Verdict(False, "descendant-escape-unexcluded")
    return Verdict(True, "managed-group-stopped")


def check(session, ref, *, crew_id, incarnation_id, root, worktree, closure=True, scope="worktree"):
    """Verify the receipt; history alone may omit closure and descendant escape proof."""
    try:
        session = _session(session)
        with _observe(_path(session, ref, ".owner")):
            with _observe(_path(session, ref, ".lock")):
                record = read(session, ref)
                if (
                    record["crew_id"],
                    record["incarnation_id"],
                    record["root"],
                    record["worktree_id"],
                ) != (crew_id, incarnation_id, str(root), worktree):
                    return Verdict(False, "identity-mismatch")
                if not record["revoked"]:
                    return Verdict(False, "not-revoked")
                if closure and record.get("close_state") not in {"not-requested", "closed"}:
                    return Verdict(False, "close-unconfirmed")
                return _shutdown_verdict(record, scope)
    except (CaptainError, OSError, KeyError, TypeError, ValueError, AttributeError):
        return Verdict(False, "missing-invalid-or-owned")


def _close_state(session, ref, state):
    with store.lock(_path(session, ref, ".lock")):
        record = read(session, ref)
        record["close_state"] = state
        _write(session, ref, record)


def close(session, ref):
    """Revoke, await bounded managed shutdown, then close the exact non-captain terminal."""
    try:
        revoke(session, ref)
        session = _session(session)
        record = read(session, ref)
        identity = {
            "crew_id": record["crew_id"],
            "incarnation_id": record["incarnation_id"],
            "root": record["root"],
            "worktree": record["worktree_id"],
        }
        if record.get("close_state") == "closed":
            # A verified close is final; its pane is gone and its name may be reused.
            return check(session, ref, **identity)
        _close_state(session, ref, "pending")
        deadline = (
            time.monotonic()
            + sum(
                _duration(name)
                for name in ("startup_timeout", "terminate_timeout", "shutdown_timeout")
            )
            + _duration("poll_interval")
        )
        while True:
            verdict = check(session, ref, **identity, closure=False, scope="group")
            if verdict.ok:
                break
            if time.monotonic() >= deadline:
                _close_state(session, ref, "failed")
                return verdict
            time.sleep(_duration("poll_interval"))
        session = _session(session)
        expected = record["pane"]
        roster = session.meta["crew"].get(record["crew_id"], {})
        captain = store.read_json(_plain(session.directory / "captain.json"), max_bytes=65536)
        if (
            not expected
            or roster.get("incarnation_id") != record["incarnation_id"]
            or captain.get("pane") == expected["pane_id"]
            or not captain.get("terminal_id")
            or captain["terminal_id"] == expected["terminal_id"]
        ):
            _close_state(session, ref, "failed")
            return Verdict(False, "unverified-terminal")
        actual = runtime.herdr("pane", "get", expected["pane_id"], timeout=5).get("pane", {})
        if any(actual.get(key) != value for key, value in expected.items()):
            _close_state(session, ref, "failed")
            return Verdict(False, "terminal-mismatch")
        runtime.herdr("pane", "close", expected["pane_id"], timeout=5)
        _close_state(session, ref, "closed")
        return check(session, ref, **identity)
    except (*runtime.HERDR_ERRORS, KeyError, TypeError, ValueError, AttributeError):
        # Leaving pending also retains when the failure itself cannot be recorded.
        try:
            _close_state(session, ref, "failed")
        except (CaptainError, OSError, KeyError, TypeError, ValueError, AttributeError):
            pass
        return Verdict(False, "close-unconfirmed")


def pending(session_directory):
    try:
        directory = Path(session_directory)
        session = _session(
            sessions.Session(
                directory, store.read_json(directory / "session.json", max_bytes=1024 * 1024)
            )
        )
        folder = _plain(directory / "lifetimes")
        for path in folder.glob("*.json"):
            record = read(session, path.stem)
            if not check(
                session,
                path.stem,
                crew_id=record["crew_id"],
                incarnation_id=record["incarnation_id"],
                root=record["root"],
                worktree=record["worktree_id"],
            ).ok:
                return True
        return False
    except (CaptainError, OSError, KeyError, TypeError, ValueError, AttributeError):
        return True


def _guard(directory, ref, control, status, mode):
    """Live group anchor: never exits voluntarily while a native writer may remain."""
    if os.getpgrp() != os.getpid():
        raise CaptainError("Managed guard must lead its own process group.")

    def send(message):
        os.write(status, json.dumps(message).encode() + b"\n")

    stopping = False

    def interrupted(*_):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        directory = Path(directory)
        session = sessions.Session(
            directory, store.read_json(directory / "session.json", max_bytes=1024 * 1024)
        )
        record = read(session, ref)
        if record["state"] != "claimed" or record["revoked"]:
            raise CaptainError("Managed guard has no active launch claim.")
        send({"type": "ready"})
        deadline = time.monotonic() + _duration("startup_timeout")
        while not select.select([control], [], [], _duration("poll_interval"))[0]:
            if stopping or time.monotonic() >= deadline:
                raise CaptainError("Managed launch handshake expired.")
        if os.read(control, 16) != b"GO\n" or stopping:
            raise CaptainError("Managed launch handshake was lost.")
        if mode != "detached" and (
            not os.isatty(0) or _foreground(0) != (os.getpgrp() if mode == "terminal" else None)
        ):
            raise CaptainError("Managed native child does not own the foreground terminal.")
        argv = list(record["argv"])
        argv[0] = runtime.executable(argv[0])
        child = subprocess.Popen(argv, cwd=record["root"], env={**os.environ, **record["env"]})
        reported = False
        scan, escape = (_DescendantScan(), None) if _DARWIN else (None, None)
        if not _DARWIN and not _LINUX:
            send({"type": "escape", "evidence": "unverified"})
        stopped_at = None
        while True:
            if select.select([control], [], [], _duration("poll_interval"))[0]:
                # EOF is the owner's lifeline, never permission to manufacture a receipt.
                if not os.read(control, 16):
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                raise CaptainError("Unexpected managed control input.")
            if not reported:
                try:
                    if os.getpgid(child.pid) != os.getpgrp():
                        raise CaptainError("Native child escaped its managed group.")
                except ProcessLookupError:
                    pass
                if stopping and stopped_at is None:
                    stopped_at = time.monotonic()
                # Reap a TERM-ignoring native ourselves, before the owner's group KILL,
                # so its exit stays authoritative for native-history cleanup.
                if stopped_at is not None and (
                    time.monotonic() - stopped_at >= _duration("terminate_timeout") / 2
                ):
                    child.kill()
                result = child.poll()
                if result is not None:
                    send({"type": "exit", "returncode": result})
                    reported = True
            if scan is not None and escape is None:
                try:
                    escape = "observed" if scan.escaped() else None
                except (OSError, AttributeError):
                    escape = "unverified"
                if escape:
                    send({"type": "escape", "evidence": escape})
    except (OSError, ValueError, CaptainError):
        try:
            send({"type": "error"})
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[5] not in {"detached", "terminal", "uncontrolled"}:
        raise SystemExit("Invalid internal managed guard invocation.")
    _guard(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
