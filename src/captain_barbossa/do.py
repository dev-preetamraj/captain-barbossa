"""Deterministic work the captain does itself, instead of recruiting a crew for it.

The write-side sibling of `inspection`, deliberately not built on it: that module's
hardened Git strips user config, hooks and credentials, which is right for reading
arbitrary paths and useless for writing. Intent rules select bounded operations;
resolution neither executes project recipes nor grants authorization.

Every run is recorded before it is reported, because work with no pane keeps the
watch-your-crew rule only by being reviewable afterwards.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, inspection, protocol
from .models import PROVIDERS, headless_argv, headless_report, resolve_model
from .runtime import CaptainError, check_text, executable, inside_project
from .store import lock, read_json, state_storage

# A declared target may run a whole test suite.
TIMEOUT = 900
# Let commands flush or clean up before a stuck process group is killed.
GROUP_TERMINATE_TIMEOUT = 1.0
# Enough tail to diagnose a failure without putting a build log in the record.
TAIL = 4000
# A headless turn reads Git through the task text, which `check_text` caps at 8000.
DIFF_LIMIT = 6000
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
# Full-input matches, in order: Git requests win over same-named Make targets.
INTENT_RULES = (
    ("git", r"(?P<operation>push|fetch|pull)", None, None, "push | fetch | pull"),
    ("commit", r"commit", "commit", None, "commit (requires message and scope)"),
    (
        "branch",
        r"(?:create|cut) a branch(?: (?P<name>\S+))?",
        "branch",
        "name",
        "create/cut a branch NAME",
    ),
    ("switch", r"switch to(?: (?P<name>\S+))?", "switch", "name", "switch to NAME"),
    (
        "run",
        r"(?:run|make)(?: (?:the )?(?P<target>\S+))?",
        "run",
        "target",
        "run/make [the] TARGET",
    ),
    (
        "target",
        r"(?:(?:do )?the )?(?P<target>\S+)",
        "run",
        "target",
        "[the] TARGET | do the TARGET",
    ),
)
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
    resolve = actions.add_parser(
        "resolve", help="resolve an operation request without executing it"
    )
    resolve.add_argument("--intent", required=True, help="the user's verbatim operation request")
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


def _terminate_process_group(process):
    """Stop and reap the command and every descendant still in its process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + GROUP_TERMINATE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.communicate(timeout=GROUP_TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # A descendant may have escaped with setsid while retaining these pipe descriptors.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        try:
            process.wait(timeout=GROUP_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


def _capture(root, command, timeout=TIMEOUT, *, text=True):
    """Run a bounded command, raising only when it could not run at all.

    Kept apart from `_spawn` so a headless turn can read stdout on its own: its result is
    JSON, which anything the tool writes to stderr would make unparseable.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CaptainError(f"{command[0]} is not installed.") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise CaptainError(f"{command[0]} exceeded {timeout}s.") from exc
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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


def _resolve_intent(text, declared_targets):
    """Resolve only the declared grammar; never run a command or infer approval."""
    result = {
        "status": "unsupported",
        "code": "unsupported-request",
        "rule": None,
        "operation": None,
        "arguments": {},
        "missing": [],
        "reason": "The whole request must match one resolvable shape.",
        "shapes": [rule[4] for rule in INTENT_RULES],
    }
    try:
        check_text(text, "intent")
        if any(ord(char) < 32 and char != "\t" for char in text):
            raise CaptainError("Intent must be a single line without control characters.")
    except CaptainError as exc:
        return {**result, "status": "invalid", "code": "invalid-intent", "reason": str(exc)}
    request = re.sub(r"[ \t]+", " ", text.strip()).removesuffix(".")
    request = re.sub(r"(?i)^please ", "", request)
    for rule, pattern, operation, field, _ in INTENT_RULES:
        match = re.fullmatch(pattern, request, re.IGNORECASE | re.ASCII)
        if not match:
            continue
        operation = operation or match["operation"].lower()
        result.update(rule=rule, operation=operation)
        if operation == "commit":
            return {
                **result,
                "status": "needs-input",
                "code": "missing-message",
                "missing": ["message", "scope"],
                "reason": "Provide a commit message and explicit paths or the existing staged scope.",
            }
        arguments = {field: match[field]} if field and match[field] else {}
        result["arguments"] = arguments
        if field:
            if field not in arguments:
                return {
                    **result,
                    "status": "needs-input",
                    "code": f"missing-{field}",
                    "missing": [field],
                    "reason": f"Provide the {field} for {operation}.",
                }
            value = arguments[field]
            try:
                if field == "name":
                    _name(value)
                elif not TARGET.fullmatch(value):
                    raise CaptainError(f"Not a usable target name: {value}")
            except CaptainError as exc:
                return {**result, "status": "invalid", "code": "invalid-name", "reason": str(exc)}
            if field == "target" and value not in declared_targets:
                return {
                    **result,
                    "code": "missing-target",
                    "reason": f"The root Makefile declares no {value} target.",
                }
        return {
            **result,
            "status": "resolved",
            "code": "resolved",
            "reason": "Resolved only; execution still requires the user's authorization.",
            "command": ["captain", "do", operation, *arguments.values()],
        }
    return result


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


def _git_bytes(root, *arguments):
    result = _capture(root, ["git", *arguments], text=False)
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


def run(args, session_directory, project):
    """Dispatch one `do` action; a refusal is recorded here, once, unless the run was.

    `resolve` executes nothing and changes no state, so it leaves no record: the run it
    leads to is the reviewable event, and a record per query would bury those.
    """
    try:
        return _dispatch(args, session_directory, project)
    except CaptainError as exc:
        if args.do_command != "resolve" and not getattr(exc, "recorded", False):
            command = ["captain", "do", args.do_command]
            record(session_directory, args.do_command, command, 1, str(exc))
        raise


def _dispatch(args, session_directory, project):
    root = Path(project).resolve(strict=True)
    action = args.do_command
    if action == "resolve":
        result = _resolve_intent(args.intent, ())
        if result["status"] == "unsupported" and result["code"] == "missing-target":
            try:
                result = _resolve_intent(args.intent, _targets(root))
            except (CaptainError, OSError) as exc:
                result.update(status="invalid", code="makefile-unavailable", reason=str(exc))
        if result["status"] == "resolved" and session_directory:
            result["command"][1:1] = ["--session", session_directory.name]
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] != "resolved":
            raise CaptainError(
                f"[{result['code']}] {result['reason']} "
                f"Resolvable shapes: {'; '.join(result['shapes'])}. "
                "Optional: please prefix and terminal period."
            )
        return
    if action == "quiet":
        note = _quiet(args, root, session_directory)
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
            command += ["--set-upstream", "origin", current]
        output = _act(root, command, session_directory, action)
        summary = f"Pushed {current}."
    else:
        if not TARGET.fullmatch(args.target):
            raise CaptainError(f"Not a usable target name: {args.target}")
        if args.target not in _targets(root):
            raise CaptainError(f"The project's Makefile declares no {args.target} target.")
        command = ["make", "--", args.target]
        output = _act(root, command, session_directory, action)
        summary = f"make {args.target} passed."
    print(summary)
    if output:
        print(output[-TAIL:])
