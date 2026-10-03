"""Deterministic work the captain does itself, instead of recruiting a crew for it.

The write-side sibling of `inspection`, deliberately not built on it: that module's
hardened Git strips user config, hooks and credentials, which is right for reading
arbitrary paths and useless for writing. Safety here comes from the other side, a fixed
verb set with no rewriting or discarding flag reachable from any of it.

Every run is recorded before it is reported, because work with no pane keeps the
watch-your-crew rule only by being reviewable afterwards.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .runtime import CaptainError, check_text

# Declared targets that only build, check or clean. Not release, publish, deploy, bump
# or version: outward-facing compound acts stay with the user.
RUNNABLE = (
    "build",
    "check",
    "clean",
    "fmt",
    "format",
    "gate",
    "install",
    "lint",
    "test",
    "typecheck",
    "vet",
)
# A declared target may run a whole test suite.
TIMEOUT = 900
# Enough tail to diagnose a failure without putting a build log in the record.
TAIL = 4000
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def add_arguments(subparsers):
    parser = subparsers.add_parser(
        "do", help="deterministic work with no crew: commit, push, branch, declared targets"
    )
    actions = parser.add_subparsers(dest="do_command", required=True)
    commit = actions.add_parser("commit", help="stage the named paths and commit them")
    commit.add_argument("--message", required=True)
    commit.add_argument("path", nargs="*", help="paths to stage; omit to commit what is staged")
    actions.add_parser("push", help="push the current branch, setting upstream on first push")
    branch = actions.add_parser("branch", help="create and switch to a new branch")
    branch.add_argument("name")
    run = actions.add_parser("run", help="run one target the project declares in its Makefile")
    run.add_argument("target", help=f"one of: {', '.join(RUNNABLE)}")


def _inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or ".git" in path.relative_to(root).parts:
        raise CaptainError(f"Path must stay inside the project, outside .git: {value}")
    return path.relative_to(root).as_posix()


def _git(root, *arguments, check=True):
    return _spawn(root, ["git", *arguments], check=check)


def _spawn(root, command, check=True):
    try:
        done = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        raise CaptainError(f"{command[0]} is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptainError(f"{' '.join(command)} exceeded {TIMEOUT}s.") from exc
    output = (done.stdout + done.stderr).strip()
    if check and done.returncode != 0:
        # A quiet action that failed silently is worse than a visible one.
        raise CaptainError(f"{' '.join(command)} failed ({done.returncode}):\n{output[-TAIL:]}")
    return done.returncode, output


def _branch(root):
    _, name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return name


def _targets(root):
    """Target names the project declares in its own Makefile."""
    makefile = root / "Makefile"
    if not makefile.is_file() or makefile.is_symlink():
        raise CaptainError("No Makefile at the project root; `do run` has nothing to call.")
    found = set()
    for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:(?!=)", line)
        if match:
            found.add(match.group(1))
    return found


def record(session_directory, action, command, code, output):
    """Append the run to the session event log, so unwatched work stays reviewable.

    Raised rather than swallowed: an unreviewable action is what this must not produce.
    """
    events = session_directory / "events" / "captain.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "quiet",
        "action": action,
        "command": command,
        "exit": code,
        "at": time.time(),
        "output": output[-TAIL:],
    }
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(args, session_directory, project):
    root = Path(project).resolve(strict=True)
    action = args.do_command
    if action == "commit":
        check_text(args.message, "message")
        paths = [_inside(root, value) for value in args.path]
        if paths:
            _git(root, "add", "--", *paths)
        code, _ = _git(root, "diff", "--cached", "--quiet", check=False)
        if code == 0:
            raise CaptainError("Nothing staged to commit; name the paths to stage.")
        command = ["git", "commit", "--message", args.message]
        code, output = _spawn(root, command)
        _, head = _git(root, "rev-parse", "--short", "HEAD")
        summary = f"Committed {head} on {_branch(root)}."
    elif action == "branch":
        if (
            not BRANCH.fullmatch(args.name)
            or ".." in args.name
            or args.name.endswith((".lock", "/"))
        ):
            raise CaptainError(f"Not a usable branch name: {args.name}")
        command = ["git", "switch", "--create", args.name]
        code, output = _spawn(root, command)
        summary = f"Created and switched to {args.name}."
    elif action == "push":
        current = _branch(root)
        if current == "HEAD":
            raise CaptainError("Detached HEAD; nothing to push.")
        tracked, _ = _git(root, "rev-parse", "--abbrev-ref", f"{current}@{{upstream}}", check=False)
        command = ["git", "push"]
        if tracked != 0:
            command += ["--set-upstream", "origin", current]
        code, output = _spawn(root, command)
        summary = f"Pushed {current}."
    else:
        if args.target not in RUNNABLE:
            raise CaptainError(
                f"{args.target} is not a quiet target. Quiet targets only build, check or "
                f"clean: {', '.join(RUNNABLE)}. Anything else needs the user."
            )
        if args.target not in _targets(root):
            raise CaptainError(f"The project's Makefile declares no {args.target} target.")
        command = ["make", args.target]
        code, output = _spawn(root, command)
        summary = f"make {args.target} passed."
    record(session_directory, action, command, code, output)
    print(summary)
    if output:
        print(output[-TAIL:])
