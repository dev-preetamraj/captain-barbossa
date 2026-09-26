"""Fixed, bounded local reads. This retains, and does not replace, the native sandbox.

Git supports ordinary, stable local checkouts only: no linked worktrees, alternate
object stores, partial clones, config includes, filters or executable configuration.
Filesystem deadlines are cooperative; a stalled filesystem still needs OS isolation.
"""

import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import memory
from .runtime import CaptainError

MAX_BYTES = 64 * 1024
MAX_ENTRIES = 10_000
MAX_RESULTS = 200
MAX_SCAN_BYTES = 8 * 1024 * 1024
TIMEOUT = 5
# /usr/bin/git on macOS is a launcher for the mutable xcode-select toolchain.
GIT = Path(
    "/Library/Developer/CommandLineTools/usr/bin/git"
    if sys.platform == "darwin"
    else "/usr/bin/git"
)


def add_arguments(subparsers):
    parser = subparsers.add_parser("inspect", help="bounded local file, state and Git reads")
    actions = parser.add_subparsers(dest="inspection_command", required=True)
    for name in ("files", "read", "search"):
        action = actions.add_parser(name)
        if name == "search":
            action.add_argument("text", help="literal, case-sensitive text")
        if name == "read":
            action.add_argument("path")
        else:
            action.add_argument("path", nargs="?", default=".")
    state = actions.add_parser("state", help="read a literal path within one stored scope")
    state.add_argument("scope", choices=("session", "project", "repo"))
    state.add_argument("path", nargs="?", default=".")
    git = actions.add_parser("git", help="fixed Git queries in an ordinary local checkout")
    git.add_argument("operation", choices=("status", "log", "current-branch", "root", "diff"))
    git.add_argument("--staged", action="store_true", help="diff the index instead of the worktree")


def _fail(code, message):
    raise CaptainError(f"Inspection {code}: {message}")


def _deadline(deadline):
    if time.monotonic() >= deadline:
        _fail("timeout", f"read exceeded {TIMEOUT} seconds.")


def _relative(root, value):
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            _fail("outside-root", "path must stay inside the selected scope.")
    if ".." in path.parts or "\0" in str(path):
        _fail("outside-root", "parent traversal and NUL bytes are not allowed.")
    return path


@contextmanager
def _open(root, path):
    """Walk from an authorized canonical root without following swapped-in symlinks."""
    path = _relative(root, path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read(root, path, limit=MAX_BYTES):
    with _open(root, path) as fd:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail("unsupported-file", "only regular files can be read.")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(8192, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    return bytes(data[:limit]), len(data) > limit


def _walk(root, path, deadline, *, metadata=False):
    pending, scanned = [path], 0
    while pending:
        _deadline(deadline)
        current = pending.pop()
        if len(current.parts) > 64:
            _fail("limit", "directory nesting exceeds 64 levels.")
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
                _fail("unsupported-file", "only regular files and directories can be inspected.")
            entries = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    _deadline(deadline)
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        _fail("limit", f"scan exceeds {MAX_ENTRIES} directory entries.")
                    if not metadata and entry.name == ".git":
                        continue
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except FileNotFoundError:
                        if metadata:
                            continue
                        raise
                    if stat.S_ISLNK(mode):
                        if metadata:
                            _fail("unsupported-git", "Git metadata contains a symlink.")
                        continue
                    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                        entries.append(current / entry.name)
                    elif metadata:
                        _fail("unsupported-git", "Git metadata contains a special file.")
            pending.extend(sorted(entries, reverse=True))


def _text(data):
    try:
        if b"\0" in data:
            raise UnicodeError
        return data.decode("utf-8")
    except UnicodeError:
        _fail("binary", "file is not UTF-8 text.")


def _files(root, path, deadline):
    paths = []
    for item in _walk(root, path, deadline):
        if len(paths) == MAX_RESULTS:
            return {"paths": paths, "truncated": True}
        paths.append(item.as_posix())
    return {"paths": paths, "truncated": False}


def _search(root, path, text, deadline):
    if not text or len(text.encode("utf-8")) > MAX_BYTES:
        _fail("invalid-text", "search text must be nonempty and at most 65536 bytes.")
    matches, scanned, truncated = [], 0, False
    for item in _walk(root, path, deadline):
        _deadline(deadline)
        data, cut = _read(root, item)
        scanned += len(data)
        if scanned > MAX_SCAN_BYTES:
            _fail("limit", "search exceeds 8 MiB of file content.")
        truncated |= cut
        try:
            content = _text(data)
        except CaptainError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if text in line:
                if len(matches) == MAX_RESULTS:
                    return {"matches": matches, "truncated": True}
                matches.append({"path": item.as_posix(), "line": number, "text": line})
    return {"matches": matches, "truncated": truncated}


def _state_root(project, scope, session_id):
    if scope == "repo":
        return memory.repo_graph(project).parent
    if scope == "project":
        return memory.read_storage(memory.state_root(), project)
    return memory.read_session(project, session_id, max_bytes=MAX_BYTES).directory


def _git_config(root):
    data, cut = _read(root, Path(".git/config"))
    if cut:
        _fail("unsupported-git", "Git config exceeds 65536 bytes.")
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
    for line in _text(data).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r'\[([a-zA-Z]+)(?: "[^"\\\x00-\x1f]+")?\]', line)
        if header:
            section = header[1].lower()
            if section not in allowed:
                _fail("unsupported-git", "Git config contains an unsupported section.")
            continue
        entry = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9-]*)\s*=\s*([^\\\x00-\x1f]*)", line)
        if not entry or entry[1].lower() not in allowed.get(section, set()):
            _fail("unsupported-git", "Git config contains an unsupported setting or syntax.")
        key, value = entry[1].lower(), entry[2].strip().lower()
        if section == "core":
            valid = value == "0" if key == "repositoryformatversion" else value in ("true", "false")
            if not valid or (key == "bare" and value != "false"):
                _fail("unsupported-git", "only ordinary format-0 worktrees are supported.")


def _git(root, operation, staged, deadline):
    if staged and operation != "diff":
        _fail("invalid-option", "--staged applies only to git diff.")
    if (root / ".git").is_symlink() or not (root / ".git").is_dir():
        _fail("unsupported-git", "an ordinary .git directory at the project root is required.")
    for path in _walk(root, Path(".git"), deadline, metadata=True):
        if (
            path.name in ("commondir", "gitdir", "config.worktree", "alternates", "http-alternates")
            or path.suffix == ".promisor"
        ):
            _fail("unsupported-git", "linked, alternate and partial object stores are unsupported.")
    _git_config(root)
    executable = GIT.resolve(strict=True)
    for item in (executable, *executable.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            _fail("unsupported-git", "Git must be a root-owned system executable.")
    env = {
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
    commands = {
        "status": [
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ],
        "log": ["log", "-20", "--no-show-signature", "--no-decorate", "--format=%H %s", "--"],
        "current-branch": ["branch", "--show-current"],
        "root": ["rev-parse", "--show-toplevel"],
        "diff": [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-submodules=all",
            "--no-color",
            *(["--cached"] if staged else []),
            "--",
        ],
    }
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
    command.extend(commands[operation])
    with subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        output, size = {process.stdout: bytearray(), process.stderr: bytearray()}, 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream in output:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    _deadline(deadline)
                    for key, _ in selector.select(max(0, deadline - time.monotonic())):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > MAX_BYTES:
                            _fail("limit", "Git output exceeds 65536 bytes.")
                        output[key.fileobj].extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode:
                _fail("git-failed", f"Git exited with status {process.returncode}.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return {
        "text": bytes(output[process.stdout]).decode("utf-8", errors="replace"),
        "truncated": False,
    }


def run(args, project):
    """Print one bounded JSON result; raise CaptainError with stable failure categories."""
    deadline = time.monotonic() + TIMEOUT
    try:
        root = Path(project).resolve(strict=True)
        command = args.inspection_command
        if command == "git":
            result = _git(root, args.operation, args.staged, deadline)
        else:
            if command == "state":
                if os.environ.get("CAPTAIN_ROLE") == "crew" and args.scope != "repo":
                    _fail("forbidden-scope", "crew may inspect only repo state.")
                root = _state_root(root, args.scope, getattr(args, "session", None))
                if not root.exists():
                    _fail("missing", "selected state scope does not exist.")
                if root.resolve() != root.absolute():
                    _fail("outside-root", "stored scope must not contain symlinks.")
            path = _relative(root, args.path)
            if command == "files":
                result = _files(root, path, deadline)
            elif command == "search":
                result = _search(root, path, args.text, deadline)
            elif command == "state" and (root / path).is_dir():
                result = _files(root, path, deadline)
            else:
                data, truncated = _read(root, path)
                result = {"path": path.as_posix(), "text": _text(data), "truncated": truncated}
        _deadline(deadline)
        encoded = json.dumps(result, sort_keys=True, ensure_ascii=True)
        if len(encoded) + 1 > MAX_BYTES:
            _fail("limit", "encoded output exceeds 65536 bytes; select a narrower path.")
        print(encoded)
    except FileNotFoundError as exc:
        raise CaptainError("Inspection missing: file, scope or system Git does not exist.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptainError("Inspection timeout: Git exceeded its deadline.") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise CaptainError(
            "Inspection unreadable: inaccessible, symlinked or unsupported path."
        ) from exc
