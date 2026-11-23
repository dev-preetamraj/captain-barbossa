"""Fixed, bounded local reads. This retains, and does not replace, the native sandbox.

Git supports ordinary checkouts and registered crew worktrees; no alternate
object stores, partial clones, config includes, filters or executable configuration.
Filesystem deadlines are cooperative; a stalled filesystem still needs OS isolation.
"""

import json
import os
import selectors
import subprocess
import time
from pathlib import Path

from . import execution, memory, protocol, sessions, store, worktrees
from .runtime import CaptainError
from .safe_read import (
    GIT,
    MAX_BYTES,
    TIMEOUT,
    check_deadline,
    decode_text,
    fail,
    git_command,
    git_environment,
    read_bytes,
    relative,
    unsupported_git_metadata,
    validate_git_config,
    walk_files,
)
from .worktrees import validate_registration

MAX_RESULTS = 200
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_PATTERN = 512


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
    git.add_argument(
        "operation",
        choices=(
            "status",
            "log",
            "current-branch",
            "root",
            "diff",
            "branches",
            "ls-files",
            "grep",
        ),
    )
    git.add_argument("--staged", action="store_true", help="diff the index instead of the worktree")
    git.add_argument("--text", help="literal, case-sensitive text for git grep")


def _outside(target):
    """An absolute path outside the project as its own canonical root and relative path."""
    if "\0" in str(target) or ".." in target.parts:
        fail("outside-root", "parent traversal and NUL bytes are not allowed.")
    # Only the parent is resolved; the leaf is still opened without following a symlink.
    root, leaf = target.parent.resolve(strict=True), Path(target.name or ".")
    if os.environ.get("CAPTAIN_ROLE") == "crew":
        target = root / leaf
        for stored in (store.state_root(), store.temp_root()):
            if protocol.overlaps(target, stored.resolve()):
                fail("forbidden-scope", "crew may inspect only repo state.")
    return root, leaf


def _files(root, path, deadline):
    paths = []
    for item in walk_files(root, path, deadline):
        if len(paths) == MAX_RESULTS:
            return {"paths": paths, "truncated": True}
        paths.append(item.as_posix())
    return {"paths": paths, "truncated": False}


def _search(root, path, text, deadline):
    if not text or len(text.encode("utf-8")) > MAX_BYTES:
        fail("invalid-text", "search text must be nonempty and at most 65536 bytes.")
    matches, scanned, truncated = [], 0, False
    for item in walk_files(root, path, deadline):
        check_deadline(deadline)
        data, cut = read_bytes(root, item)
        scanned += len(data)
        if scanned > MAX_SCAN_BYTES:
            fail("limit", "search exceeds 8 MiB of file content.")
        truncated |= cut
        try:
            content = decode_text(data)
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
        return store.read_storage(store.state_root(), project)
    return sessions.read_session(project, session_id, max_bytes=MAX_BYTES).directory


def git_text(root, operation, *, staged=False):
    """One hardened Git read for another module, as plain text."""
    return _git(Path(root), operation, staged, time.monotonic() + TIMEOUT)["text"]


def _git(root, operation, staged, deadline, text=None, registration=None):
    if staged and operation != "diff":
        fail("invalid-option", "--staged applies only to git diff.")
    if operation == "grep":
        if not text or len(text.encode("utf-8")) > MAX_PATTERN:
            fail("invalid-text", f"git grep needs literal text of 1 to {MAX_PATTERN} bytes.")
    elif text is not None:
        fail("invalid-option", "--text applies only to git grep.")
    metadata_root = root
    if registration is not None:
        validate_registration(registration, deadline=deadline)
        if root != Path(registration["execution_root"]):
            fail("unsupported-git", "registered execution root does not match.")
        metadata_root = Path(registration["project"])
    elif (root / ".git").is_symlink() or not (root / ".git").is_dir():
        fail("unsupported-git", "an ordinary or registered .git directory is required.")
    for path in walk_files(metadata_root, Path(".git"), deadline, metadata=True):
        if unsupported_git_metadata(path):
            fail("unsupported-git", "linked, alternate and partial object stores are unsupported.")
    validate_git_config(metadata_root)
    executable = GIT.resolve(strict=True)
    for item in (executable, *executable.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            fail("unsupported-git", "Git must be a root-owned system executable.")
    env = git_environment()
    commands = {
        "status": [
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ],
        "log": ["log", "-20", "--no-show-signature", "--no-decorate", "--format=%H %s", "--"],
        "current-branch": ["branch", "--show-current"],
        "branches": ["branch", "--list", "--no-color", "--format=%(refname:short)"],
        # Tracked files only, so .venv, caches and build output stay out of the answer that
        # `files` and `search` have to walk past.
        "ls-files": ["ls-files", "--cached", "--"],
        "grep": [
            "grep",
            "--fixed-strings",
            "--line-number",
            "--no-color",
            "-I",
            # -e keeps a pattern that starts with a dash a pattern.
            "-e",
            text or "",
            "--",
        ],
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
    command = [*git_command(executable), *commands[operation]]
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
                    check_deadline(deadline)
                    for key, _ in selector.select(max(0, deadline - time.monotonic())):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > MAX_BYTES:
                            fail("limit", "Git output exceeds 65536 bytes.")
                        output[key.fileobj].extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            # git grep reports "nothing matched" as status 1, which is an answer, not a fault.
            if process.returncode and not (operation == "grep" and process.returncode == 1):
                fail("git-failed", f"Git exited with status {process.returncode}.")
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
        registration = None
        if command != "state" and os.environ.get("CAPTAIN_WORKTREE_ID"):
            current = sessions.read_session(root, getattr(args, "session", None))
            root = execution.current(root, current.meta["id"], deadline=deadline)
            registration = worktrees.read(
                current, os.environ["CAPTAIN_WORKTREE_ID"], validate=False, deadline=deadline
            )
        if command == "git":
            result = _git(root, args.operation, args.staged, deadline, args.text, registration)
        else:
            if command == "state":
                if os.environ.get("CAPTAIN_ROLE") == "crew" and args.scope != "repo":
                    fail("forbidden-scope", "crew may inspect only repo state.")
                root = _state_root(root, args.scope, getattr(args, "session", None))
                if not root.exists():
                    fail("missing", "selected state scope does not exist.")
                if root.resolve() != root.absolute():
                    fail("outside-root", "stored scope must not contain symlinks.")
            target = Path(args.path)
            if command != "state" and target.is_absolute() and not target.is_relative_to(root):
                root, path = _outside(target)
            else:
                path = relative(root, args.path)
            if command == "files":
                result = _files(root, path, deadline)
            elif command == "search":
                result = _search(root, path, args.text, deadline)
            elif command == "state" and (root / path).is_dir():
                result = _files(root, path, deadline)
            else:
                data, truncated = read_bytes(root, path)
                result = {
                    "path": path.as_posix(),
                    "text": decode_text(data),
                    "truncated": truncated,
                }
        check_deadline(deadline)
        encoded = json.dumps(result, sort_keys=True, ensure_ascii=True)
        if len(encoded) + 1 > MAX_BYTES:
            fail("limit", "encoded output exceeds 65536 bytes; select a narrower path.")
        print(encoded)
    except FileNotFoundError as exc:
        raise CaptainError("Inspection missing: file, scope or system Git does not exist.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptainError("Inspection timeout: Git exceeded its deadline.") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise CaptainError(
            "Inspection unreadable: inaccessible, symlinked or unsupported path."
        ) from exc
