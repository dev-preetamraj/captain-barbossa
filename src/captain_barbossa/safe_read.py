"""Bounded, no-follow reads of project files and a hardened read-only Git, shared by inspect and worktrees."""

import os
import re
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .runtime import CaptainError

MAX_BYTES = 64 * 1024


MAX_ENTRIES = 10_000


TIMEOUT = 5


SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "target",
    ".next",
    "node_modules",
}


# /usr/bin/git on macOS is a launcher for the mutable xcode-select toolchain.
GIT = Path(
    "/Library/Developer/CommandLineTools/usr/bin/git"
    if sys.platform == "darwin"
    else "/usr/bin/git"
)


def fail(code, message):
    raise CaptainError(f"Inspection {code}: {message}")


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        fail("timeout", f"read exceeded {TIMEOUT} seconds.")


def relative(root, value):
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            fail("outside-root", "path must stay inside the selected scope.")
    if ".." in path.parts or "\0" in str(path):
        fail("outside-root", "parent traversal and NUL bytes are not allowed.")
    return path


@contextmanager
def _open_root(root):
    """Open an absolute directory without following a symlink in any component."""
    root = Path(root)
    if not root.is_absolute():
        fail("outside-root", "inspection roots must be absolute.")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open(root, path):
    """Walk from an authorized canonical root without following swapped-in symlinks."""
    path = relative(root, path)
    with _open_root(root) as root_fd:
        fd = root_fd
        try:
            for index, part in enumerate(path.parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(path.parts) - 1:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=fd)
                if fd != root_fd:
                    os.close(fd)
                fd = next_fd
            yield fd
        finally:
            if fd != root_fd:
                os.close(fd)


def read_bytes(root, path, limit=MAX_BYTES):
    with _open(root, path) as fd:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            fail("unsupported-file", "only regular files can be read.")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(limit + 1)
    return data[:limit], len(data) > limit


def walk_files(root, path, deadline, *, metadata=False):
    pending, scanned = [path], 0
    while pending:
        check_deadline(deadline)
        current = pending.pop()
        if len(current.parts) > 64:
            fail("limit", "directory nesting exceeds 64 levels.")
        with ExitStack() as stack:
            try:
                fd = stack.enter_context(_open(root, current))
            except FileNotFoundError:
                # Git maintenance can remove discovered metadata before we open it.
                if metadata and current != path:
                    continue
                raise
            mode = os.fstat(fd).st_mode
            if stat.S_ISREG(mode):
                yield current
                continue
            if not stat.S_ISDIR(mode):
                fail("unsupported-file", "only regular files and directories can be inspected.")
            entries = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    check_deadline(deadline)
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        fail("limit", f"scan exceeds {MAX_ENTRIES} directory entries.")
                    if not metadata and entry.name in SKIP_DIRS:
                        continue
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except FileNotFoundError:
                        if metadata:
                            continue
                        raise
                    if stat.S_ISLNK(mode):
                        if metadata:
                            fail("unsupported-git", "Git metadata contains a symlink.")
                        continue
                    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                        entries.append(current / entry.name)
                    elif metadata:
                        fail("unsupported-git", "Git metadata contains a special file.")
            pending.extend(sorted(entries, reverse=True))


def decode_text(data):
    try:
        if b"\0" in data:
            raise UnicodeError
        return data.decode("utf-8")
    except UnicodeError:
        fail("binary", "file is not UTF-8 text.")


def validate_git_config(root):
    data, cut = read_bytes(root, Path(".git/config"))
    if cut:
        fail("unsupported-git", "Git config exceeds 65536 bytes.")
    # Accept a small literal subset; never ask Git to parse includes before validation.
    allowed = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        },
        "remote": {"url", "fetch"},
        "branch": {"remote", "merge", "vscode-merge-base"},
        "user": {"name", "email"},
    }
    section = None
    for line in decode_text(data).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r'\[([a-zA-Z]+)(?: "[^"\\\x00-\x1f]+")?\]', line)
        if header:
            section = header[1].lower()
            if section not in allowed:
                fail("unsupported-git", "Git config contains an unsupported section.")
            continue
        entry = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9-]*)\s*=\s*([^\\\x00-\x1f]*)", line)
        if not entry or entry[1].lower() not in allowed.get(section, set()):
            fail("unsupported-git", "Git config contains an unsupported setting or syntax.")
        key, value = entry[1].lower(), entry[2].strip().lower()
        if section == "core":
            valid = value == "0" if key == "repositoryformatversion" else value in ("true", "false")
            if not valid or (key == "bare" and value != "false"):
                fail("unsupported-git", "only ordinary format-0 worktrees are supported.")


def unsupported_git_metadata(path):
    return (
        (path.name in ("commondir", "gitdir") and path.parts[:2] != (".git", "worktrees"))
        or path.name in ("config.worktree", "alternates", "http-alternates")
        or path.suffix == ".promisor"
    )


def git_environment():
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/dev/null",
        "XDG_CONFIG_HOME": "/dev/null",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def git_command(executable):
    command = [str(executable), "--no-pager", "--no-optional-locks"]
    for setting in (
        "core.fsmonitor=false",
        "core.hooksPath=/dev/null",
        "core.attributesFile=/dev/null",
        "protocol.allow=never",
        "maintenance.auto=false",
        "gc.auto=0",
    ):
        command.extend(("-c", setting))
    return command
