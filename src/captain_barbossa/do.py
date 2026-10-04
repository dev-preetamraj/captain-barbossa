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
import sys
import time
from pathlib import Path

from . import config, inspection
from .models import PROVIDERS, headless_argv, headless_report, resolve_model
from .runtime import CaptainError, check_text, executable, inside_project
from .store import read_json

# Declared targets that only build, check, clean, or describe the project. Not release,
# publish, deploy, bump or version: outward-facing compound acts stay with the user, and
# `version` is a bump as often as a read.
RUNNABLE = (
    "build",
    "check",
    "clean",
    "fmt",
    "format",
    "gate",
    "help",
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
# A headless turn reads Git through the task text, which `check_text` caps at 8000.
DIFF_LIMIT = 6000
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
# Every shipped provider has a headless turn and a gate that stops it writing, each in its
# own shape: models.headless_argv builds the argv, models.headless_report reads the result.
# A provider without one would need its own list here, and the refusal below is for it.
# The prompt below must not restate that gate: a codex turn reads through the shell alone,
# so telling it not to run commands leaves it nothing to read with and it refuses the task.
QUIET_PROMPT = (
    "You are a one-shot agent for Captain Barbossa: do the task and finish. Nobody is "
    "watching, so there is nobody to ask and no pane to open; read whatever the task needs "
    "with the tools you have. Your final message is the whole report; nobody will read a "
    "terminal. Leave every edit unstaged."
)


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
    switch = actions.add_parser("switch", help="switch to a branch that already exists")
    switch.add_argument("name")
    actions.add_parser("fetch", help="fetch from the remote, touching no file in the worktree")
    actions.add_parser(
        "pull", help="fast-forward the current branch; never merges, rebases or conflicts"
    )
    run = actions.add_parser("run", help="run one target the project declares in its Makefile")
    run.add_argument("target", help=f"one of: {', '.join(RUNNABLE)}")
    quiet = subparsers.add_parser(
        "quiet", help="one headless model turn with no pane: a question, a review, a bounded edit"
    )
    # The dispatcher reads do_command alone, so quiet needs no second branch in cli.main.
    quiet.set_defaults(do_command="quiet")
    quiet.add_argument("--task", required=True)
    quiet.add_argument(
        "--diff",
        choices=("worktree", "staged"),
        help="append this diff to the task; a headless turn cannot read Git itself",
    )
    quiet.add_argument(
        "--write",
        action="append",
        default=[],
        metavar="PATH",
        help="a file the turn may edit; read-only when omitted",
    )
    quiet.add_argument("--model", help="tier or model name ([crew] model when omitted)")
    quiet.add_argument(
        "--timeout", type=float, metavar="SECONDS", help=f"seconds to allow (default {TIMEOUT})"
    )


def _git(root, *arguments, check=True):
    return _spawn(root, ["git", *arguments], check=check)


def _capture(root, command, timeout=TIMEOUT):
    """Run a bounded command, raising only when it could not run at all.

    Kept apart from `_spawn` so a headless turn can read stdout on its own: its result is
    JSON, which anything the tool writes to stderr would make unparseable.
    """
    try:
        return subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        raise CaptainError(f"{command[0]} is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptainError(f"{command[0]} exceeded {timeout}s.") from exc


def _spawn(root, command, check=True):
    done = _capture(root, command)
    output = (done.stdout + done.stderr).strip()
    if check and done.returncode != 0:
        # A quiet action that failed silently is worse than a visible one.
        raise CaptainError(f"{' '.join(command)} failed ({done.returncode}):\n{output[-TAIL:]}")
    return done.returncode, output


def _act(root, command, session_directory, action):
    """Run the action's command, recorded whether or not it succeeded.

    A failed action is the one most worth reviewing, so the record is written before the
    error is raised rather than after a success that may never come.
    """
    code, output = _spawn(root, command, check=False)
    record(session_directory, action, command, code, output)
    if code != 0:
        # A quiet action that failed silently is worse than a visible one.
        raise CaptainError(f"{' '.join(command)} failed ({code}):\n{output[-TAIL:]}")
    return output


def _branch(root):
    _, name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return name


def _name(value):
    """A branch name no option, traversal or lock file can hide in."""
    if not BRANCH.fullmatch(value) or ".." in value or value.endswith((".lock", "/")):
        raise CaptainError(f"Not a usable branch name: {value}")
    return value


def _quiet(args, root, session_directory):
    """One headless model turn: no pane, no assignment, no wait, a result read afterwards.

    A pane buys somewhere to approve a prompt and somewhere to steer a running turn. Work
    needing neither pays for neither; the bargain `do` already makes holds here too, so an
    unwatched turn stays acceptable by leaving its edits unstaged and its report recorded.
    """
    check_text(args.task, "task")
    marker = session_directory / "captain.json"
    if not marker.exists():
        raise CaptainError("No captain in this session to borrow a CLI from; start captain first.")
    provider = read_json(marker).get("provider")
    if provider not in PROVIDERS:
        raise CaptainError(
            f"{provider or 'This captain'} has no headless turn to spend, so there is no way "
            "to bound what an unwatched one could do. Recruit crew instead."
        )
    paths = [inside_project(root, value, "Path") for value in args.write]
    wanted = args.model or config.text("crew", "model")
    # The prompt leads the task instead of riding a per-provider system-prompt flag: three
    # sentences in front of a one-shot turn behave the same and need no fourth code path.
    task = f"{QUIET_PROMPT}\n\n{args.task}"
    if paths:
        task += "\n\nEdit only these files, and leave the edits unstaged: " + ", ".join(paths)
    if args.diff:
        # Only codex's turn has a shell, and --diff behaves the same on all four.
        text = inspection.git_text(root, "diff", staged=args.diff == "staged")
        task += f"\n\nThe {args.diff} diff:\n{text[:DIFF_LIMIT]}"
        if len(text) > DIFF_LIMIT:
            task += "\n(diff truncated)"
    model = resolve_model(provider, wanted) if wanted else None
    command = headless_argv(
        provider,
        executable(provider),
        task,
        writable=bool(paths),
        # A read-only turn may also read this session's own state: the event log, mail and
        # assignment records it is asked to triage or check a report against. A writing turn
        # is never handed the directory, so it cannot edit them.
        add_dir=None if paths else str(session_directory),
        model=model,
    )
    done = _capture(root, command, args.timeout or TIMEOUT)
    report, note = headless_report(provider, done.stdout, model)
    record(session_directory, "quiet", command, done.returncode, report)
    print(report)
    return note


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
    if action == "quiet":
        note = _quiet(args, root, session_directory)
        if note:
            print(note, file=sys.stderr)
        return
    if action == "commit":
        check_text(args.message, "message")
        paths = [inside_project(root, value, "Path") for value in args.path]
        if paths:
            _git(root, "add", "--", *paths)
        code, _ = _git(root, "diff", "--cached", "--quiet", check=False)
        if code == 0:
            raise CaptainError("Nothing staged to commit; name the paths to stage.")
        command = ["git", "commit", "--message", args.message]
        output = _act(root, command, session_directory, action)
        _, head = _git(root, "rev-parse", "--short", "HEAD")
        summary = f"Committed {head} on {_branch(root)}."
    elif action == "branch":
        command = ["git", "switch", "--create", _name(args.name)]
        output = _act(root, command, session_directory, action)
        summary = f"Created and switched to {args.name}."
    elif action == "switch":
        # Git refuses on its own when the switch would overwrite local changes, and no
        # --force or --discard-changes is reachable from here.
        command = ["git", "switch", _name(args.name)]
        output = _act(root, command, session_directory, action)
        summary = f"Switched to {args.name}."
    elif action == "fetch":
        command = ["git", "fetch"]
        output = _act(root, command, session_directory, action)
        summary = "Fetched."
    elif action == "pull":
        # Fast-forward only: it either moves the ref or exits non-zero, so a merge this
        # cannot resolve never starts. The merging form is not a flag anyone can pass.
        command = ["git", "pull", "--ff-only"]
        output = _act(root, command, session_directory, action)
        summary = f"Fast-forwarded {_branch(root)}."
    elif action == "push":
        current = _branch(root)
        if current == "HEAD":
            raise CaptainError("Detached HEAD; nothing to push.")
        tracked, _ = _git(root, "rev-parse", "--abbrev-ref", f"{current}@{{upstream}}", check=False)
        command = ["git", "push"]
        if tracked != 0:
            command += ["--set-upstream", "origin", current]
        output = _act(root, command, session_directory, action)
        summary = f"Pushed {current}."
    else:
        if args.target not in RUNNABLE:
            raise CaptainError(
                f"{args.target} is not a quiet target. Quiet targets only build, check, clean "
                f"or describe the project: {', '.join(RUNNABLE)}. Anything else needs the user."
            )
        if args.target not in _targets(root):
            raise CaptainError(f"The project's Makefile declares no {args.target} target.")
        command = ["make", args.target]
        output = _act(root, command, session_directory, action)
        summary = f"make {args.target} passed."
    print(summary)
    if output:
        print(output[-TAIL:])
