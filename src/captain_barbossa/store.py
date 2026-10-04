"""Where captain state lives on disk, and how it is read and written.

Roots, the per-project namespace hash, atomic writes and the file lock. This is the
bottom of the package: it imports nothing but `.runtime`, so anything may import it.
"""

import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .runtime import CaptainError


def project_root():
    cwd = Path.cwd().resolve()
    if os.environ.get("CAPTAIN_PROJECT"):
        project = Path(os.environ["CAPTAIN_PROJECT"]).resolve()
        if not cwd.is_relative_to(project):
            raise CaptainError("This directory is outside the active captain project.")
        return project
    for directory in (cwd, *cwd.parents):
        if (directory / ".git").exists():
            return directory
    return cwd


def private_dir(path):
    if path.is_symlink():
        raise CaptainError(f"Memory directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise CaptainError(f"Memory directory belongs to another user: {path}")
    path.chmod(0o700)
    return path


def _root(env_var, default):
    # CAPTAIN_STATE_ROOT/CAPTAIN_TEMP_ROOT let a launched child pin the exact roots its
    # parent resolved, so a default (no CAPTAIN_MEMORY_ROOT) parent doesn't get collapsed
    # onto one root when re-resolving XDG_STATE_HOME/tempfile defaults in the child's env.
    value = os.environ.get(env_var, os.environ.get("CAPTAIN_MEMORY_ROOT", default))
    return Path(value).expanduser().absolute()


def temp_root():
    """Ephemeral root for session data; the OS may reclaim it between reboots."""
    return _root(
        "CAPTAIN_TEMP_ROOT", str(Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}")
    )


_warned_temp_state_root = False


def state_root():
    """Durable root for project-scope graph.json; survives OS temp cleanup."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    root = _root("CAPTAIN_STATE_ROOT", str(base / "captain-barbossa"))
    global _warned_temp_state_root
    if not _warned_temp_state_root and root.is_relative_to(Path(tempfile.gettempdir())):
        # A captain launched before the state/temp split exported one root into its
        # children, so the "durable" graph lands where the OS reclaims it.
        _warned_temp_state_root = True
        print(
            f"captain: durable memory root {root} is inside the OS temp directory; "
            "restart the captain so project memory outlives temp cleanup.",
            file=sys.stderr,
        )
    return root


def rooted_storage(root, project):
    project = project.resolve()
    if root.resolve().is_relative_to(project):
        raise CaptainError("Memory root must be outside the project repository.")
    private_dir(root)
    project_id = hashlib.sha256(os.fsencode(project)).hexdigest()
    return private_dir(root / project_id)


def storage(project):
    return rooted_storage(temp_root(), project)


def state_storage(project):
    return rooted_storage(state_root(), project)


def read_storage(root, project):
    """Resolve an existing storage namespace without mkdir, chmod, locks or migration."""
    project = project.resolve()
    if root.resolve().is_relative_to(project):
        raise CaptainError("Memory root must be outside the project repository.")
    path = root / hashlib.sha256(os.fsencode(project)).hexdigest()
    for item in (root, path):
        if item.is_symlink():
            raise CaptainError(f"Memory directory must not be a symlink: {item}")
    return root.resolve() / path.name


def read_json(path, *, max_bytes=None):
    try:
        if max_bytes is not None:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise CaptainError("Memory read requires a regular file.")
                data = file.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise CaptainError(f"Memory read exceeds {max_bytes} bytes.")
            return json.loads(data)
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CaptainError(f"Cannot read memory at {path}: {exc}") from exc


def read_cursor(path):
    """The stored offset into a crew's event log; a missing cursor means read from the start."""
    return read_json(path) if path.exists() else 0


def write_text(path, text):
    fd, name = tempfile.mkstemp(prefix=".captain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield
