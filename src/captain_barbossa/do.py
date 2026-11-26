"""Deterministic work the captain does itself, instead of recruiting a crew for it.

The write-side sibling of `inspection`, deliberately not built on it: that module's
hardened Git strips user config, hooks and credentials, which is right for reading
arbitrary paths and useless for writing.

Every run is recorded before it is reported, because work with no pane keeps the
watch-your-crew rule only by being reviewable afterwards.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

from . import config, inspection, protocol
from .eventlog import record_action
from .models import PROVIDERS, headless_argv, headless_report, resolve_model
from .processes import TARGET, TIMEOUT, capture, makefile_targets
from .runtime import CaptainError, check_text, executable, inside_project
from .store import lock, read_json, state_storage

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
# In a supervised session these verbs leave the machine or run arbitrary recipes, so each run
# needs the user's approved authorization request; the rest need one only through the app.
CATEGORIES = {"push": "outward", "fetch": "outward", "pull": "outward", "run": "destructive"}
# Set by the supervisor on the one run an approved request authorizes.
AUTHORIZATION_ENV = "CAPTAIN_AUTHORIZATION"
# An approval unspent this long expires, so a stale yes never authorizes a later run.
APPROVAL_TTL = 900
FIELDS = ("do_command", "message", "path", "name", "target", "task", "diff", "write")
FIELDS += ("read", "model", "timeout")
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
    run.add_argument("target", help="one literal target declared in the root Makefile")
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
    quiet.add_argument(
        "--read",
        action="append",
        default=[],
        metavar="DIR",
        help="an extra directory a read-only turn may read, outside the checkout",
    )
    quiet.add_argument("--model", help="tier or model name ([crew] model when omitted)")
    quiet.add_argument(
        "--timeout", type=float, metavar="SECONDS", help=f"seconds to allow (default {TIMEOUT})"
    )


def _git(root, *arguments, check=True):
    return _spawn(root, ["git", *arguments], check=check)


def _spawn(root, command, check=True):
    done = capture(root, command)
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
        error = CaptainError(f"{' '.join(command)} failed ({code}):\n{output[-TAIL:]}")
        error.recorded = True
        raise error
    return output


def _branch(root):
    _, name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return name


def _name(value):
    """A branch name no option, traversal or lock file can hide in."""
    if not BRANCH.fullmatch(value) or ".." in value or value.endswith((".lock", "/")):
        raise CaptainError(f"Not a usable branch name: {value}")
    return value


def _quiet(args, root, session_directory, meta=None):
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
    if meta is None:
        path = session_directory / "session.json"
        meta = read_json(path) if path.exists() else {}
    denials = (meta.get("plugins") or {}).get("denials")
    if denials:
        from .plugins import refuse

        refuse(denials, provider, "quiet")
    paths = protocol.owned_paths(root, args.write)
    outside_files = [path for path in paths if Path(path).is_absolute() and not Path(path).is_dir()]
    if outside_files:
        raise CaptainError(
            "[outside-file] quiet --write cannot grant an outside file without granting its "
            "unowned parent directory; recruit crew with an owned outside directory instead."
        )
    if paths and args.read:
        raise CaptainError("--read is for a read-only turn; drop it or --write.")
    reads = [str((root / value).resolve()) for value in args.read]
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
        # is never handed the directory, so it cannot edit them; it gets only the
        # directories of its write paths outside the checkout.
        add_dirs=(
            protocol.outside_dirs(root, paths) if paths else [str(session_directory), *reads]
        ),
        model=model,
        # A denial is enforced by pi having no shell tool, so its extensions must not add one.
        isolated=bool(denials),
    )
    timeout = (
        TIMEOUT
        if args.timeout is None
        else config.in_range(args.timeout, "--timeout", below=float("inf"))
    )
    done = capture(root, command, timeout)
    report, note = headless_report(provider, done.stdout, model)
    record(session_directory, "quiet", command, done.returncode, report)
    print(report)
    return note


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


def _git_bytes(root, *arguments):
    result = capture(root, ["git", *arguments], text=False)
    if result.returncode:
        raise CaptainError(os.fsdecode(result.stderr)[-TAIL:])
    return result.stdout


def _index_baseline(root):
    return (
        _git_bytes(root, "ls-files", "--stage", "-z"),
        _git(root, "rev-parse", "--verify", "HEAD", check=False),
        _git(root, "symbolic-ref", "--quiet", "HEAD", check=False),
    )


def _commit_scope(root, paths):
    def changed(*options):
        output = _git_bytes(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            *options,
            "--",
        )
        return {os.fsdecode(path) for path in output.split(b"\0") if path}

    if changed("--diff-filter=U"):
        raise CaptainError("[unmerged-index] Resolve index conflicts before committing.")
    staged = changed("--cached")
    if paths:
        outside = sorted(
            name for name in staged if not any(Path(name).is_relative_to(path) for path in paths)
        )
        if outside:
            raise CaptainError(f"[staged-outside-scope] Staged paths outside scope: {outside!r}")
        untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
        mixed = sorted(
            staged & (changed() | {os.fsdecode(path) for path in untracked.split(b"\0") if path})
        )
        if mixed:
            raise CaptainError(
                f"[mixed-origin-changes] Both staged and unstaged changes: {mixed!r}. "
                "Select the intended hunks explicitly before committing."
            )
    return staged


def _commit(args, root, session_directory):
    check_text(args.message, "message")
    paths = [inside_project(root, value, "Path") for value in args.path]
    baseline = _index_baseline(root)
    # Captain commits cooperate on this lock; native Git does not acquire it.
    with lock(state_storage(root) / "git.lock"):
        if _index_baseline(root) != baseline:
            raise CaptainError("[index-changed] Index or HEAD changed while waiting; retry.")
        existing = _commit_scope(root, paths)
        if _index_baseline(root) != baseline:
            raise CaptainError("[index-changed] Index or HEAD changed during scope checks; retry.")
        # A staged deletion no longer matches an add pathspec; preserve it as staged.
        to_stage = [path for path in paths if path not in existing or (root / path).exists()]
        if to_stage:
            _git(root, "--literal-pathspecs", "add", "--", *to_stage)
        staged = _index_baseline(root)
        if staged[1:] != baseline[1:] or (not paths and staged != baseline):
            raise CaptainError("[index-changed] Index or HEAD changed before commit; retry.")
        if not _commit_scope(root, paths):
            raise CaptainError("[empty-index] Nothing staged to commit; name the paths to stage.")
        if _index_baseline(root) != staged:
            raise CaptainError("[index-changed] Index or HEAD changed before commit; retry.")
        output = _act(
            root, ["git", "commit", "--message", args.message], session_directory, "commit"
        )
        _, head = _git(root, "rev-parse", "--short", "HEAD")
        return f"Committed {head} on {_branch(root)}.", output


def parse(argv):
    """A `do` or `quiet` argv parsed exactly as the CLI parses it."""

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise CaptainError(f"[authorization-argv] {message}")

    parser = Parser(prog="captain", add_help=False)
    add_arguments(parser.add_subparsers(dest="command", parser_class=Parser))
    if not argv or argv[0] not in ("do", "quiet"):
        raise CaptainError("[authorization-argv] Only do and quiet need an authorization.")
    return parser.parse_args(argv)


def command_of(args):
    """The fields that make two runs the same command, in a fixed order."""
    return {field: getattr(args, field) for field in FIELDS if hasattr(args, field)}


def category(args):
    return CATEGORIES.get(args.do_command, "local")


def _expire(rows, now):
    for row in rows:
        if row["state"] == "approved" and now - row["resolved_at"] > APPROVAL_TTL:
            row["state"] = "expired"


def _log(session_directory, row, **fields):
    record_action(
        session_directory,
        "authorization",
        "captain",
        None,
        supervised=True,
        request=row["id"],
        state=row["state"],
        **fields,
    )


def request_authorization(session_directory, args, origin, argv=None):
    """The open or approved request for this exact command, else a new open one."""
    wanted = command_of(args)
    with protocol.checkpoint(session_directory) as state:
        rows = state.setdefault("authorizations", [])
        _expire(rows, time.time())
        found = next(
            (
                row
                for row in rows
                if row["command"] == wanted and row["state"] in ("open", "approved")
            ),
            None,
        )
        if found is not None:
            return dict(found)
        row = {
            "id": uuid4().hex,
            "origin": origin,
            "argv": argv,
            "command": wanted,
            "category": category(args),
            "state": "open",
            "at": time.time(),
        }
        rows.append(row)
    _log(session_directory, row, origin=origin, argv=argv, command=wanted, category=row["category"])
    return dict(row)


def resolve_authorization(session_directory, request_id, decision, reason=None):
    """Approve an open request for one run, or deny it."""
    if decision not in ("approve", "deny"):
        raise CaptainError("[authorization-decision] Decide approve or deny.")
    if reason is not None:
        check_text(reason, "reason")
    with protocol.checkpoint(session_directory) as state:
        rows = state.get("authorizations", [])
        _expire(rows, time.time())
        row = next((row for row in rows if row["id"] == request_id), None)
        if row is None:
            raise CaptainError(f"[authorization-unknown] No authorization request {request_id}.")
        if row["state"] != "open":
            raise CaptainError(f"[authorization-closed] Request {request_id} is {row['state']}.")
        row.update(
            state="approved" if decision == "approve" else "denied",
            reason=reason,
            resolved_at=time.time(),
        )
        row = dict(row)
    _log(session_directory, row, reason=reason)
    return row


def _spend(session_directory, args, request_id):
    """Spend the approved request for this exact command (the named one when given), or None."""
    wanted = command_of(args)
    with protocol.checkpoint(session_directory) as state:
        rows = state.get("authorizations", [])
        _expire(rows, time.time())
        row = next(
            (
                row
                for row in rows
                if row["state"] == "approved"
                and row["command"] == wanted
                and request_id in (None, row["id"])
            ),
            None,
        )
        if row is None:
            return None
        row.update(state="spent", spent_at=time.time())
        row = dict(row)
    _log(session_directory, row)
    return row


def authorize(args, session_directory):
    """Refuse a supervised run that needs the user's approval and has none to spend."""
    named = os.environ.get(AUTHORIZATION_ENV)
    if not named and category(args) == "local":
        return
    if _spend(session_directory, args, named):
        return
    if named:
        raise CaptainError(
            f"[authorization-required] Request {named} is not an approved request for this command."
        )
    row = request_authorization(session_directory, args, "captain")
    raise CaptainError(
        f"[authorization-required] `{args.do_command}` is {row['category']}: the user must "
        f"approve request {row['id']} in the app. Run the same command again once it is approved."
    )


def run(args, session_directory, project, meta=None):
    """Dispatch one `do` action; record a refusal once unless the run already was."""
    try:
        if (meta or {}).get("transport") == "supervisor":
            authorize(args, session_directory)
        return _dispatch(args, session_directory, project, meta)
    except KeyboardInterrupt:
        command = ["captain", "do", args.do_command]
        record(session_directory, args.do_command, command, 130, "Interrupted.")
        raise
    except CaptainError as exc:
        if not getattr(exc, "recorded", False):
            command = ["captain", "do", args.do_command]
            record(session_directory, args.do_command, command, 1, str(exc))
        raise


def _dispatch(args, session_directory, project, meta=None):
    root = Path(project).resolve(strict=True)
    action = args.do_command
    if action == "quiet":
        note = _quiet(args, root, session_directory, meta)
        if note:
            print(note, file=sys.stderr)
        return
    if action == "commit":
        summary, output = _commit(args, root, session_directory)
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
            _, remotes = _git(root, "remote")
            names = remotes.splitlines()
            command += ["--set-upstream", names[0] if len(names) == 1 else "origin", current]
        else:
            _, remote = _git(
                root, "for-each-ref", "--format=%(push:remotename)", f"refs/heads/{current}"
            )
            command += [remote, current]
        output = _act(root, command, session_directory, action)
        summary = f"Pushed {current}."
    else:
        if not TARGET.fullmatch(args.target):
            raise CaptainError(f"Not a usable target name: {args.target}")
        if args.target not in makefile_targets(root):
            raise CaptainError(f"The project's Makefile declares no {args.target} target.")
        command = ["make", "--", args.target]
        output = _act(root, command, session_directory, action)
        summary = f"make {args.target} passed."
    print(summary)
    if output:
        print(output[-TAIL:])
