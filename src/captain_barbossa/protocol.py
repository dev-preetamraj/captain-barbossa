"""Explicit assignment state and acknowledged delivery, local to one session."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import background_state, lifetime
from .approval import REASONS, Request, Verdict, decide, protected, recipe_flag
from .crew import Crew, event_status
from .eventlog import MAX_EVENT_BYTES, read_events
from .execution import root
from .models import HOOK_DELIVERED, HOOKLESS
from .pane import (
    DRAFT_GATE,
    HOLD_EXPIRY,
    UNREADABLE_EXPIRY,
    UNREADABLE_GATE,
    startup_screen_name,
)
from .processes import TARGET, capture, makefile_targets
from .runtime import HERDR_ERRORS, CaptainError, check_text, inside_project
from .sessions import session_directories
from .store import lock, read_json, state_root, state_storage, temp_root, write_json

ACTIONS = ("read", "search", "edit", "test", "build", "format", "commit", "version", "delete")
WRITE_ACTIONS = ("edit", "format", "commit", "version", "delete")
# Our own ring is still painting for about this long, so a draft seen inside it is ours.
ECHO_GRACE = 2
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PermissionDecision")
# Landed mail that has started no turn by then needs a look, whatever the provider.
ACTIVATION_WINDOW = 60
ATTENTION_LINES = 20
GONE = re.compile(r"\b(pane_not_found|agent_not_found)\b")
# Claude reports approvals through its PermissionRequest hook; these have no such event.
MODAL_PROVIDERS = ("codex", *HOOKLESS)
MODAL_INTERVAL = 30
# No daemon drains held mail; the wait loop already polls, so it retries the doorbell instead.
DRAIN_INTERVAL = 30
# Native startup can await hook trust before Herdr registers the crew.
REGISTRATION_GRACE = 300
# A ring that landed needs no retry for the crew's sake; this only exists so a pane that died
# after the nudge eventually bounces its mail instead of sitting on it forever.
DRAIN_LANDED_INTERVAL = 600
# A draft gate can hold mail all session, so one hold this long is escalated to the captain.
HELD_NOTICE_INTERVAL = 300
# Codex sends SessionStart only as its first turn begins, so a startup screen (update, hook
# trust) holds the ring instead; untrusted hooks fall back to native on-request prompts.
STARTUP_GATE = "startup screen"
HOOKS_UNTRUSTED = (
    "[codex-hooks-untrusted] Codex hooks untrusted: user must trust them in the pane. "
    "Mail stays queued behind the startup screen; do not type into the pane."
)
UPDATE_PROMPT = (
    "[codex-update-prompt] Codex is held at its update prompt: the user must answer it in the "
    "pane. Mail stays queued behind the startup screen; do not type into the pane."
)
HOOKS_NOT_LIVE = (
    "[codex-hooks-not-live] Codex hooks not live: a turn ran with no SessionStart, so the "
    "permission gate and Stop hook are off and the crew falls back to native prompts. "
    "The user must review and trust the hooks in the pane."
)
# A report's commit hashes: hex with a digit and a letter, so prose numbers stay out. Length 32
# is skipped because every captain id is a uuid4 hex of exactly that length.
COMMIT_TOKEN = re.compile(
    r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])(?:[0-9a-f]{7,31}|[0-9a-f]{33,40})\b"
)
QUESTION_INTERVAL = 300
# Task text: an unquoted uppercase marker, a multi-word or SHOUTED angle slot, or a {{slot}}.
# Tags, generics, <a@b.c> and quoted or lowercase marker words are prose about code.
PLACEHOLDER = re.compile(
    r"(?<![\w`'\"])(?:TODO|TBD|FIXME)(?![\w`'\"])"
    r"|<(?:[A-Za-z]+(?:[ _-][A-Za-z]+)+|[A-Z][A-Z_]+)>|\{\{[^{}]+\}\}"
)
PATH_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|FIXME)\b|<[^<>]+>|\{\{[^{}]+\}\}", re.I)
GIT_TIMEOUT = 5
# Enough of a --verify run's output to see why it failed; the full log is on disk.
VERIFY_LINES = 15
VERIFY_MARK = "[verify]"
UNSAVED_CAVEAT = "Git cannot see unsaved edits still inside the crew's native session."


@contextmanager
def checkpoint(directory, timeout=None):
    # ponytail: one session lock; split by assignment only if contention matters.
    with lock(directory / "protocol.lock", timeout):
        path = directory / "protocol.json"
        state = read_json(path) if path.exists() else {"active": {}, "assignments": {}}
        yield state
        write_json(path, state)


def protected_paths():
    """Captain state and settings and native CLI config homes: owning one could rewrite
    grants, hooks or allow rules."""
    home = Path.home()
    homes = {
        "CLAUDE_CONFIG_DIR": home / ".claude",
        "CODEX_HOME": home / ".codex",
        "PI_CODING_AGENT_DIR": home / ".pi",
        "GROK_HOME": home / ".grok",
    }
    paths = [state_root(), temp_root(), home / ".captain", home / ".claude.json"]
    for variable, default in homes.items():
        paths += [default, Path(os.environ.get(variable) or default).expanduser()]
    return [path.resolve() for path in paths]


def _case_path(value):
    return Path(*(part.casefold() for part in Path(value).parts))


def owned_path(root, value):
    """Root-relative inside root, else the resolved absolute path; never .git, /, home,
    an ancestor of root, captain state or a native CLI config home."""
    path = (root / value).resolve()
    if any(part.casefold() in {".git", ".captain"} for part in path.parts):
        raise CaptainError(
            f"[protected-path] Owned path must not be .git, .captain, /, home or a project "
            f"ancestor: {value}"
        )
    if path.is_relative_to(root):
        return inside_project(root, value, "Owned path")
    if _case_path(root).is_relative_to(_case_path(path)) or _case_path(
        Path.home().resolve()
    ).is_relative_to(_case_path(path)):
        raise CaptainError(
            f"[protected-path] Owned path must not be .git, /, home or a project ancestor: {value}"
        )
    if any(overlaps(path, protected) for protected in protected_paths()):
        raise CaptainError(
            f"[protected-path] Owned path must not touch captain state or a native CLI home: {value}"
        )
    return path.as_posix()


def owned_paths(project, paths):
    root = Path(project).resolve()
    return sorted({owned_path(root, value) for value in paths})


def outside_dirs(root, paths):
    """Owned outside directories safe for native --add-dir flags."""
    allowed = []
    for directory in {Path(path) for path in paths}:
        if not directory.is_absolute() or not directory.is_dir():
            continue
        try:
            allowed.append(owned_path(Path(root).resolve(), directory))
        except CaptainError:
            pass
    return sorted(allowed)


def overlaps(left, right):
    a, b = _case_path(left), _case_path(right)
    return a.is_relative_to(b) or b.is_relative_to(a)


def project_assignments(project, state, directory):
    """Every assignment of the project: this session's from state, the rest from disk."""
    assignments = list(state["assignments"].values())
    for other in session_directories(project):
        path = other / "protocol.json"
        if other != directory and path.exists():
            assignments.extend(read_json(path)["assignments"].values())
    return assignments


def held_by_other(project, state, directory, assignment_id, root, paths):
    """Whether another unfinished assignment owns any of paths (the flagged-crew test)."""
    return any(
        overlaps(a, b)
        for other in project_assignments(project, state, directory)
        if other["id"] != assignment_id and other["state"] != "done"
        for a in paths
        for b in other["paths"]
        if Path(a).is_absolute() or Path(other.get("execution_root", project)).resolve() == root
    )


def occupant(project, directory, paths):
    """The first unfinished assignment, in any checkout, whose owned paths overlap paths.

    Owned paths are root-relative, so a worktree crew's count too: a shared edit to the
    same file would still collide when its result is applied.
    """
    path = directory / "protocol.json"
    state = read_json(path) if path.exists() else {"assignments": {}}
    mine = owned_paths(project, paths)
    for other in project_assignments(project, state, directory):
        if other["state"] != "done" and any(overlaps(a, b) for a in mine for b in other["paths"]):
            return other
    return None


def require_current(crew):
    """Pre-incarnation crew ran on retired pane heuristics; their files stay on disk."""
    if not crew.record.get("incarnation_id"):
        raise CaptainError(
            f"Legacy crew {crew.display_name} predates this captain and can no longer be "
            "driven; dismiss it and recruit a fresh crew."
        )


def active(state, crew, assignment_id=None, incarnation=None):
    require_current(crew)
    key = state["active"].get(crew.crew_id)
    assignment = state["assignments"].get(key)
    if not assignment:
        raise CaptainError("Crew has no protocol assignment; recruit new crew explicitly.")
    if assignment["incarnation_id"] != crew.record["incarnation_id"]:
        raise CaptainError("Stale crew incarnation.")
    if assignment_id is not None and assignment_id != key:
        # Name the live one: a crew whose launch-bound id went stale at handoff has no
        # other way to discover what replaced it.
        raise CaptainError(
            f"Stale assignment ID {assignment_id}; this crew's live assignment is {key}. "
            f"Retry with --assignment {key}."
        )
    if incarnation is not None and incarnation != assignment["incarnation_id"]:
        raise CaptainError("Stale crew incarnation.")
    return assignment


def begin(
    crew, project, task, paths=(), actions=(), handoff=None, *, assignment_id=None, verify=None
):
    if not isinstance(task, str) or not task.strip():
        raise CaptainError("[empty-goal] Assignment goal must not be empty.")
    check_text(task, "task")
    if PLACEHOLDER.search(task) or any(PATH_PLACEHOLDER.search(str(path)) for path in paths):
        raise CaptainError("[placeholder] Replace assignment placeholder text.")

    execution_root = root(crew.session, crew.record)
    if verify is not None:
        check_target(execution_root, verify)
    if crew.record.get("worktree_id") and assignment_id != crew.record.get("assignment_id"):
        raise CaptainError("[worktree-handoff] Isolated assignment identity is immutable.")
    if handoff and crew.record.get("worktree_id"):
        raise CaptainError("[worktree-handoff] Recruit a fresh isolated crew for a new assignment.")
    inherit_paths, inherit_actions = not paths, not actions
    paths = owned_paths(execution_root, paths)
    if crew.record.get("worktree_id") and any(Path(path).is_absolute() for path in paths):
        raise CaptainError("[worktree-outside] A worktree cannot isolate paths outside it.")
    actions = sorted(set(actions) | {"read", "search"})
    if set(actions) - set(ACTIONS):
        raise CaptainError("Unknown assignment action.")
    require_current(crew)
    # One lock for the project, whichever root a session lives in.
    with (
        lock(state_storage(project) / "allocation.lock"),
        checkpoint(crew.session.directory) as state,
    ):
        previous = state["active"].get(crew.crew_id)
        if previous:
            old = state["assignments"][previous]
            if not handoff and old["incarnation_id"] != crew.record["incarnation_id"]:
                # The name was reused after its former incarnation was dismissed; that
                # incarnation released the name, so a plain recruit starts fresh.
                previous = None
            elif old["state"] != "done" or handoff != previous:
                raise CaptainError(
                    "Reassignment requires done with report and --handoff ASSIGNMENT_ID."
                )
            elif old["pending"] or old["ack_seq"] != len(old["notices"]):
                raise CaptainError(
                    "Acknowledge the previous assignment notifications before handoff."
                )
        if handoff and not previous:
            raise CaptainError("No previous assignment matches this handoff.")
        if handoff:
            inherited = state["assignments"][handoff]
            if inherit_paths:
                paths = inherited["paths"]
            if inherit_actions:
                actions = inherited["actions"]
        launched_dirs = crew.record.get("outside_dirs")
        if launched_dirs is not None and outside_dirs(execution_root, paths) != launched_dirs:
            raise CaptainError(
                "[launch-directories] Assignment outside directories differ from crew launch; "
                "recruit fresh crew."
            )
        if "edit" in actions and not paths:
            raise CaptainError("[missing-owned-paths] Edit actions require owned paths.")
        if verify is not None and "test" not in actions:
            raise CaptainError("[verify-needs-test] --verify runs a target; grant --allow test.")
        for other in project_assignments(project, state, crew.session.directory):
            if other["state"] != "done" and any(
                overlaps(a, b)
                for a in paths
                for b in other["paths"]
                # Absolute paths are the same file from every execution root.
                if Path(a).is_absolute()
                or Path(other.get("execution_root", project)).resolve() == execution_root
            ):
                raise CaptainError(
                    f"[owned-path-conflict] Owned paths overlap active assignment {other['id']}."
                )
        assignment = {
            "id": assignment_id or uuid4().hex,
            "execution_root": str(execution_root),
            # A claimed path stays claimed, so it never becomes delete-eligible on the successor.
            "claimed": list(state["assignments"][handoff].get("claimed", ())) if handoff else [],
            "worktree_id": crew.record.get("worktree_id"),
            "incarnation_id": crew.record["incarnation_id"],
            "crew": crew.display_name,
            "original_task": task,
            "paths": paths,
            "actions": actions,
            "state": "working",
            "question": None,
            "report": None,
            "messages": [],
            "notices": [],
            "ack_seq": 0,
            "offset": crew.events.stat().st_size if crew.events.exists() else 0,
            "pending": None,
            "last_ack": None,
            "native_status": None,
            "verify": verify,
        }
        state["assignments"][assignment["id"]] = assignment
        state["active"][crew.crew_id] = assignment["id"]
    return assignment


def notice(assignment, status, summary):
    assignment["notices"].append({"status": status, "summary": summary})


def validate_actor(crew, args):
    assignment_id = getattr(args, "assignment", None)
    incarnation = getattr(args, "incarnation", None)
    if os.environ.get("CAPTAIN_ROLE") == "crew":
        if os.environ.get("CAPTAIN_CREW", "").casefold() != crew.crew_id.casefold():
            raise CaptainError("Crew may act only on its own assignment.")
        if not incarnation or incarnation != os.environ.get("CAPTAIN_INCARNATION"):
            raise CaptainError("Crew incarnation is required.")
        if not assignment_id and args.command in ("ask", "done", "check"):
            assignment_id = os.environ.get("CAPTAIN_ASSIGNMENT")
    if not assignment_id:
        raise CaptainError("An explicit --assignment ID is required.")
    return assignment_id, incarnation


def check_action(assignment, project, action, paths, held=lambda paths: False):
    if assignment["state"] == "done":
        raise CaptainError("Assignment is already done.")
    if action not in assignment["actions"]:
        raise CaptainError(f"[action-not-granted] Action {action!r} was not granted.")
    if action in WRITE_ACTIONS:
        if not paths:
            raise CaptainError("This action requires explicit owned paths.")
        for path in owned_paths(project, paths):
            if any(Path(path).is_relative_to(Path(owner)) for owner in assignment["paths"]):
                continue
            # The edit hook auto-claims unowned, unprotected checkout files.
            if (
                action == "edit"
                and not Path(path).is_absolute()
                and not protected(Path(path).parts)
            ):
                if held([path]):
                    raise CaptainError(f"[flagged-crew] Path is owned by another crew: {path}")
                continue
            raise CaptainError(f"[outside-grant] Path is not owned by this assignment: {path}")


def _git(project, *args):
    """Read-only git in the project, or None when there is no readable work tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result


def unlanded(project, assignment, *, ignore_uncommitted=False):
    """Acknowledged reports may retain dirty paths, but still require verifiable commits."""
    # Paths outside the checkout are not Git's to vouch for; dismiss names them instead.
    paths = [path for path in assignment["paths"] if not Path(path).is_absolute()]
    if not paths:
        return []
    inside = _git(project, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode or inside.stdout.strip() != "true":
        return [f"[no-git] cannot verify {', '.join(paths)}"]
    reasons = []
    status = _git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths)
    if status is None or status.returncode:
        reasons.append(f"[git-failed] status of {', '.join(paths)}")
    else:
        fields, changed = iter(status.stdout.split("\0")), set()
        for entry in fields:
            if entry:
                changed.add(entry[3:])
                if "R" in entry[:2] or "C" in entry[:2]:
                    # -z gives a rename or copy its source path as the next, unprefixed field.
                    next(fields, None)
        if changed and not ignore_uncommitted:
            reasons.append(f"[uncommitted] {', '.join(sorted(changed))}")
    missing, unverified = [], []
    for token in dict.fromkeys(COMMIT_TOKEN.findall(assignment.get("report") or "")):
        found = _git(project, "rev-parse", "--verify", "--quiet", f"{token}^{{commit}}")
        if found is None or found.returncode not in (0, 1):
            unverified.append(token)
        elif found.returncode:
            missing.append(token)
        else:
            refs = _git(project, "for-each-ref", "--count=1", "--contains", found.stdout.strip())
            if refs is None or refs.returncode:
                unverified.append(token)
            elif not refs.stdout.strip():
                missing.append(token)
    if unverified:
        reasons.append(f"[git-failed] cannot verify commit {', '.join(unverified)}")
    if missing:
        reasons.append(f"[unreachable-commit] {', '.join(missing)}")
    return reasons


def grant(crew, assignment_id, command):
    """Captain-only: let this assignment run one exact command text once. The gate spends it
    on the first exact match; a handoff starts a new assignment, so it is never inherited."""
    if os.environ.get("CAPTAIN_ROLE") == "crew":
        raise CaptainError("grant is captain-only; crew run captain ask instead.")
    if not isinstance(command, str) or not command.strip():
        raise CaptainError("[empty-grant] Grant one exact, non-empty command.")
    check_text(command, "command")
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        if assignment["state"] == "done":
            raise CaptainError("Assignment is done; grant on its live successor instead.")
        grants = assignment.setdefault("grants", [])
        if not any(entry["command"] == command for entry in grants):
            grants.append({"command": command, "at": time.time()})
        return {"assignment_id": assignment["id"], "grants": len(grants)}


def change(crew, args, project):
    assignment_id, incarnation = validate_actor(crew, args)
    if args.command == "done":
        # `done` is the crew's last action, so it runs before the Stop hook and a refusal
        # here pre-empts that delivery. Hand the mail over rather than naming a command.
        deliver_before_done(crew)
        check_text(args.report, "report")
        # Run outside the lock: a test suite must not stall every other protocol verb.
        receipt = verified(crew, assignment_id)
    with checkpoint(crew.session.directory) as state:
        # `ask` binds to the live assignment, not the launch-bound id: it is the crew's
        # only way out, and a stale id used to block it. Everything else keeps the explicit
        # id, so a report never attaches to an assignment the captain replaced.
        assignment = active(
            state, crew, None if args.command == "ask" else assignment_id, incarnation
        )
        if args.command == "check":
            if lifetime.revoked(crew.session, crew.record):
                raise CaptainError("[action-not-granted] Crew lifetime was revoked.")
            execution_root = root(crew.session, crew.record)
            check_action(
                assignment,
                execution_root,
                args.action,
                args.path,
                lambda paths: held_by_other(
                    project,
                    state,
                    crew.session.directory,
                    assignment["id"],
                    Path(execution_root).resolve(),
                    paths,
                ),
            )
            return {"assignment_id": assignment["id"], "allowed": True}
        if assignment["state"] == "done":
            raise CaptainError("Assignment is already done.")
        if args.command == "ask":
            check_text(args.question, "question")
            if assignment["question"]:
                raise CaptainError("Only one question may be pending.")
            question = {
                "id": uuid4().hex,
                "text": args.question,
                "due_at": time.time() + QUESTION_INTERVAL,
                "reasked": False,
                "escalated": False,
            }
            assignment["question"] = question
            assignment["state"] = "asked"
            notice(assignment, "asked", f"{question['id']}: {args.question}")
            return {"assignment_id": assignment["id"], "question_id": question["id"]}
        if assignment["question"]:
            raise CaptainError("Answer the pending question before done.")
        if any(message["delivery"] == "prepared" for message in assignment["messages"]):
            raise CaptainError("[delivery-pending] Retry the prepared message before done.")
        if unread(crew):
            # Raced past deliver_before_done; the retry hands it over.
            raise CaptainError("Mail arrived while this report was being filed; retry done.")
        assignment["report"] = args.report
        assignment["state"] = "done"
        if receipt:
            assignment["verification"] = receipt
        notice(assignment, "done", args.report)
        response = {"assignment_id": assignment["id"], "status": "done"}
        if receipt:
            response["verification"] = receipt
        return response


def check_target(root, target):
    """A target validated as `captain do run` validates one."""

    if not isinstance(target, str) or not TARGET.fullmatch(target):
        raise CaptainError(f"Not a usable target name: {target}")
    if target not in makefile_targets(Path(root)):
        raise CaptainError(f"The project's Makefile declares no {target} target.")


def verified(crew, assignment_id):
    """Run the assignment's --verify target; its failure is recorded, never a refusal.

    `done` is a protocol verb the crew gate grants outright, so the run must earn the
    verdict the gate would give this crew for the same make command, judged now, against
    the Makefile as it stands. The recipe check also covers `make test`, which the gate
    grants by name.
    """

    assignment = active_assignment(crew)
    if (
        not assignment
        or assignment["id"] != assignment_id
        or assignment["state"] == "done"
        or not assignment.get("verify")
    ):
        return None
    target = assignment["verify"]
    root = str(Path(assignment["execution_root"]).resolve())
    # The judged argv is the argv run; begin's target check rules out an option.
    command = ["make", target]
    gate = decide(
        Request(
            "Bash",
            root,
            root,
            tuple(assignment["paths"]),
            tuple(assignment["actions"]),
            command=shlex.join(command),
        )
    )
    refused = None if gate.verdict == Verdict.ALLOW_ONCE else gate.code
    if lifetime.revoked(crew.session, crew.record):
        refused = "action-not-granted"
    refused = refused or recipe_flag(root, [target])
    if refused:
        return {"target": target, "exit": None, "seconds": 0, "output": None, "refused": refused}
    log = crew.session.directory / "verify" / f"{assignment_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        # Leave time for process cleanup and the receipt within the crew's 10-minute call.
        ran = capture(root, command, timeout=540)
        code, output = ran.returncode, ran.stdout + ran.stderr
    except (CaptainError, OSError) as exc:
        code, output = None, str(exc)
    log.write_text(output, encoding="utf-8")
    return {
        "target": target,
        "exit": code,
        "seconds": round(time.monotonic() - started, 1),
        "output": str(log),
        "tail": output.strip().splitlines()[-VERIFY_LINES:],
    }


def done_summary(assignment):
    """The crew's report, with the captain-run receipt first when one was recorded.

    Report and make output are crew-controlled, so they are quoted under the receipt, and
    a report line that leads with the marker is quoted even without one: only the
    captain's own receipt can start a line with VERIFY_MARK.
    """
    report = assignment["report"]
    receipt = assignment.get("verification")
    if not receipt:
        return "\n".join(
            f"| {line}" if line.lstrip().casefold().startswith(VERIFY_MARK) else line
            for line in report.splitlines()
        )
    if receipt.get("refused"):
        outcome = f"NOT RUN [{receipt['refused']}]"
    elif receipt["exit"] == 0:
        outcome = "passed"
    else:
        outcome = f"FAILED (exit {receipt['exit']})"
    head = f"{VERIFY_MARK} make {receipt['target']} {outcome}"
    if receipt.get("output"):
        head += f" in {receipt['seconds']}s; output: {receipt['output']}"
    return "\n".join(
        [
            head,
            *(f"  | {line}" for line in receipt.get("tail", [])),
            "Crew report:",
            *(f"  | {line}" for line in report.splitlines()),
        ]
    )


def deliver_before_done(crew):
    """Hand over mail that arrived mid-turn, as the refusal itself, then refuse.

    Called outside the protocol lock, because the receipt takes it.
    """
    messages = unread(crew)
    if not messages:
        return
    # A body the ring already typed reached the crew; receipt it rather than re-serve it.
    landed = landed_bodies(crew)
    pending = [message for message in messages if message["id"] not in landed]
    body = render(crew.session.directory, pending)
    receipt(crew.session.directory, crew.crew_id, [message["id"] for message in messages])
    if not pending:
        return
    raise CaptainError(
        f"{body}\n\nThat mail arrived before this report. Act on it, then run done again."
    )


def mail_dir(directory, crew_id):
    return directory / "mail" / crew_id


def enqueue(crew, text, kind, assignment_id, message_id=None):
    """Write a durable mail record; caller must already hold protocol.lock (deliver's checkpoint)."""
    message = {
        "id": message_id or uuid4().hex,
        "assignment_id": assignment_id,
        "incarnation_id": crew.record.get("incarnation_id"),
        "kind": kind,
        "text": text,
        "queued_at": time.time(),
        "state": "queued",
        "read_at": None,
    }
    directory = mail_dir(crew.session.directory, crew.crew_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{message['id']}.json"
    if path.exists():
        existing = read_json(path)
        if any(
            existing.get(key) != message[key]
            for key in ("id", "assignment_id", "incarnation_id", "kind", "text")
        ):
            raise CaptainError("[message-id-conflict] Message ID has different content.")
    else:
        write_json(path, message)
    return message["id"]


def queued(session_directory, crew_id):
    """Queued mail, oldest first; the Stop hook reads it without a crew object."""
    directory = mail_dir(session_directory, crew_id)
    if not directory.exists():
        return []
    record = read_json(session_directory / "session.json").get("crew", {}).get(crew_id)
    if not record or record.get("status") == "dismissed":
        return []
    messages = (read_json(path) for path in directory.glob("*.json"))
    return sorted(
        (
            m
            for m in messages
            if m["state"] == "queued" and m.get("incarnation_id") == record.get("incarnation_id")
        ),
        key=lambda m: m["queued_at"],
    )


def unread(crew):
    return queued(crew.session.directory, crew.crew_id)


def receipt(session_directory, crew_id, ids, started=False):
    """The one writer of `read`, for the crew's own `inbox` and for a delivery hook.

    started: the delivering hook itself began or continued a turn (UserPromptSubmit, a
    blocking Stop). Its event precedes this receipt, so the log after landing cannot show it.
    """
    directory = mail_dir(session_directory, crew_id)
    receipts = {}
    with lock(session_directory / "protocol.lock"):
        for message_id in ids:
            path = directory / f"{message_id}.json"
            message = read_json(path)
            if message["state"] == "queued":
                message["state"] = "read"
                message["read_at"] = time.time()
                write_json(path, message)
                receipts.setdefault(message.get("assignment_id"), []).append(message_id)
    # checkpoint takes protocol.lock too, so mirror the receipt after the mail writes, like bounce.
    if receipts:
        with checkpoint(session_directory) as state:
            for assignment_id, read_ids in receipts.items():
                assignment = state["assignments"].get(assignment_id)
                for message in assignment["messages"] if assignment else ():
                    # A second read finds the mail already off "queued", so nothing to mirror.
                    if message["id"] in read_ids and message["delivery"] == "sent":
                        message["delivery"] = "read"
                        events = f"{crew_id}-{assignment['incarnation_id']}.jsonl"
                        message.setdefault("landed", landing(session_directory / "events" / events))
                        if started:
                            message["activated"] = True


def landing(events):
    """When a body reached the crew and how far its event log ran: later turns are its own."""
    return {"at": time.time(), "offset": events.stat().st_size if events.exists() else 0}


def render(session_directory, messages):
    """The one rendering of mail for a model, shared by the hook, `inbox` and the ring."""
    if not messages:
        return None
    state = read_json(session_directory / "protocol.json")
    # Queued mail belongs to the crew's one active assignment; the first names it.
    assignment = state["assignments"].get(messages[0].get("assignment_id"))
    header = f"{identity(assignment)}\n\n" if assignment else ""
    bodies = "\n\n".join(message["text"] for message in messages)
    return f"Mail from the captain:\n\n{header}{bodies}"


def identity(assignment):
    """The crew's standing facts, rendered at read time so a follow-up never re-sends them."""
    return (
        f"Crew name: {assignment['crew']}.\n"
        f"Assignment {assignment['id']}; incarnation {assignment['incarnation_id']}.\n"
        f"Owned paths: {', '.join(assignment['paths']) or '(none)'}. "
        f"Allowed actions: {', '.join(assignment['actions'])}."
    )


def bounce(crew, message_id, reason):
    """Undeliverable mail is reported, never parked; the next wait names it."""
    directory = crew.session.directory
    with lock(directory / "protocol.lock"):
        path = mail_dir(directory, crew.crew_id) / f"{message_id}.json"
        message = read_json(path)
        message["state"] = "bounced"
        message["reason"] = reason
        write_json(path, message)
    with checkpoint(directory) as state:
        assignment = state["assignments"].get(state["active"].get(crew.crew_id))
        if assignment:
            notice(
                assignment,
                "bounced",
                f"Mail to {crew.display_name} bounced ({message_id}): {reason}",
            )


def stale_draft(prior, gate, draft):
    """Whether a composer hold is old; age never authorizes typing into it."""
    if not prior or prior.get("draft_gate") != gate or prior.get("draft") != draft:
        return False
    since = prior.get("draft_since")
    return since is not None and time.time() - since >= HOLD_EXPIRY[gate]


def echoing(prior):
    """Whether a ring landed so recently that the composer may still be painting it."""
    return bool(prior) and prior.get("landed") and time.time() - prior["at"] < ECHO_GRACE


def awaiting_registration(crew, message_id, prior, error, assignment=None):
    """Only an unread initial task may wait for a missing startup registration."""
    if not isinstance(error, CaptainError) or not (
        re.search(r"\bagent_not_found\b", str(error))
        or str(error) == f"{crew.record['agent']} is not registered."
    ):
        return False
    if prior and prior["id"] == message_id and prior["landed"]:
        return False
    message = next((m for m in unread(crew) if m["id"] == message_id), None)
    assignment = assignment if assignment is not None else active_assignment(crew)
    return bool(
        message
        and message["kind"] == "assignment"
        and message.get("incarnation_id") == crew.record.get("incarnation_id")
        and assignment
        and assignment["id"] == message["assignment_id"]
        and assignment["incarnation_id"] == crew.record.get("incarnation_id")
        and 0 <= time.time() - message["queued_at"] < REGISTRATION_GRACE
        and not any(
            turn_event(event)
            for event in read_events(
                crew.events, assignment.get("offset", 0), max_bytes=MAX_EVENT_BYTES
            )[0]
        )
    )


def ring(crew, message_id):
    """Best-effort doorbell: a held gate or a broken doorbell waits for the next drain.

    Missing startup registration gets a bounded grace; other confirmed Herdr failures
    bounce the mail. A broken nudge is the harness, not the crew, so it holds instead.
    Mail is already durable, so no outcome here can lose the message.
    """
    prior = last_ring(crew)
    provider = crew.record.get("provider")
    landed, unreachable, gate, draft, held = False, None, None, None, None
    # A hook-delivered crew gets the bare wake line; its own hook supplies the body. A
    # landed retry is content-free too, so the 600s drain never retypes work.
    text = None
    if provider not in HOOK_DELIVERED and not (
        prior and prior["id"] == message_id and prior["landed"]
    ):
        # The ring is this crew's only channel, so it carries the rendered body: it may
        # act straight off the ring without ever running `inbox`.
        message = next((m for m in unread(crew) if m["id"] == message_id), None)
        text = render(crew.session.directory, [message] if message else [])
    try:
        pane = crew.pane
        gate = pane.nudge_block(crew)
        # Codex's update and hook-trust screens hide the composer; name that hold so it is
        # reported once, and drain retries it like any other.
        if gate is not None and provider == "codex" and (screen := pane.startup_screen()):
            gate = STARTUP_GATE
            if "update" in screen.casefold():
                once(crew, "startup_update", UPDATE_PROMPT)
            else:
                once(crew, "hooks_untrusted", HOOKS_UNTRUSTED)
        if gate == DRAFT_GATE and echoing(prior):
            # Our own echo, not a human. Leave the prior stamp alone and let drain retry,
            # rather than starting a hold clock against a crew that has its doorbell.
            return
        if gate in HOLD_EXPIRY:
            held, draft = gate, pane.draft(provider)
        if gate is None:
            pane.nudge(crew, text)
            landed = True
    except HERDR_ERRORS as exc:
        if awaiting_registration(crew, message_id, prior, exc):
            gate = "awaiting registration"
        else:
            unreachable = str(exc)
    except Exception:
        pass
    # The one record of a ring, so drain can tell a held doorbell from one the crew already got.
    now = time.time()
    record = {"id": message_id, "at": now, "landed": landed, "gate": gate}
    # Every body that landed, carried forward so a later held ring cannot erase one.
    bodies = list(prior.get("bodies", ())) if prior else []
    if landed and text and message_id not in bodies:
        bodies.append(message_id)
    if bodies:
        record["bodies"] = bodies
    if held is not None:
        # Preserve hold age for diagnostics, never as permission to type.
        carried = (
            prior.get("draft_since")
            if prior and prior.get("draft_gate") == held and prior.get("draft") == draft
            else None
        )
        record["draft"], record["draft_since"] = draft, carried or now
        record["draft_gate"] = held
    if not landed:
        held_over = bool(prior) and prior["id"] == message_id and not prior["landed"]
        record["held_since"] = (prior.get("held_since") or prior["at"]) if held_over else now
        record["noticed"] = bool(held_over and prior.get("noticed"))
        if (
            gate in HOLD_EXPIRY
            and not record["noticed"]
            and now - record["held_since"] >= HELD_NOTICE_INTERVAL
        ):
            record["noticed"] = held_notice(crew, message_id, gate, now - record["held_since"])
    write_json(drain_stamp(crew), record)
    if landed and text:
        with checkpoint(crew.session.directory) as state:
            assignment = state["assignments"].get(state["active"].get(crew.crew_id))
            for message in assignment["messages"] if assignment else ():
                if message["id"] == message_id:
                    message.setdefault("landed", landing(crew.events))
    if unreachable is not None:
        bounce(crew, message_id, unreachable)


def hooks_live(crew, assignment, events):
    """Codex fires SessionStart as its first turn begins; a turn that ends without one since
    the assignment began means its hooks never ran. Noticed once per assignment.

    A handoff keeps the native session, whose SessionStart predates the new offset, so the
    incarnation's whole log is consulted once before noticing."""
    for event in events:
        kind = event.get("hook_event_name", event.get("type"))
        if kind == "SessionStart":
            assignment["session_started"] = True
        elif (
            kind == "agent-turn-complete"
            and event_status(event) == "done"  # not Codex's own title turn
            and not assignment.get("session_started")
        ):
            if assignment.get("hooks_not_live"):
                return
            earlier, _ = read_events(crew.events, 0, max_bytes=MAX_EVENT_BYTES)
            if any(e.get("hook_event_name") == "SessionStart" for e in earlier):
                assignment["session_started"] = True
                continue
            assignment["hooks_not_live"] = True
            notice(assignment, "error", HOOKS_NOT_LIVE)
            return


def once(crew, key, summary):
    """Post one error notice per assignment for a startup condition, never a second, and
    none while a needs_attention episode has already shown the captain this screen."""
    with checkpoint(crew.session.directory) as state:
        assignment = state["assignments"].get(state["active"].get(crew.crew_id))
        if assignment and not assignment.get(key):
            assignment[key] = True
            if not assignment.get("attention"):
                notice(assignment, "error", summary)


def attend_startup(crew):
    """Recruitment found the native agent blocked: queue the needs_attention a wait would,
    opening the same episode, so neither wait nor the held ring reports it again."""
    sample, screen = herdr_status(crew), visible(crew)
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew)
        attention = attend(crew, assignment, sample, time.time(), screen)
        if attention:
            notice(assignment, *attention)


def held_notice(crew, message_id, gate, held):
    """Escalate a long hold to the captain once; the gate is a human's draft, never typed over."""
    with checkpoint(crew.session.directory) as state:
        assignment = state["assignments"].get(state["active"].get(crew.crew_id))
        if assignment:
            notice(
                assignment,
                "held",
                f"Mail to {crew.display_name} ({message_id}) has waited about "
                f"{round(held / 60)} min behind the {gate} gate and stays queued; "
                f"the crew's composer has to clear before the doorbell lands.",
            )
        return assignment is not None


def active_assignment(crew):
    """The crew's live assignment, read without the lock where a stale view is tolerable."""
    path = crew.session.directory / "protocol.json"
    if not path.exists():
        return None
    state = read_json(path)
    return state["assignments"].get(state["active"].get(crew.crew_id))


def turn_event(event):
    """Whether a native event proves a turn ran, not merely that a session exists."""
    return event.get("hook_event_name", event.get("type")) not in (None, "SessionStart")


def started(crew, message, live=None, at=0):
    """Whether the crew began a turn after this message landed. Each message needs its own:
    a turn for an earlier one proves nothing about a follow-up.

    A ring-delivered body also counts the crew's own receipt (inbox, done) and Herdr's
    working since it landed. A delivery hook stamps its receipt with no turn, and Claude
    can read working while it boots, so a hook-delivered crew needs a native turn event.
    """
    landed = message["landed"]
    if crew.record.get("provider") not in HOOK_DELIVERED and (
        message["delivery"] == "read" or (live == "working" and at >= landed["at"])
    ):
        return True
    events, _ = read_events(crew.events, landed["offset"], max_bytes=MAX_EVENT_BYTES)
    # Codex's own title-generation turn is not the crew's; event_status drops it too.
    return any(
        turn_event(event)
        and not (event.get("type") == "agent-turn-complete" and event_status(event) is None)
        for event in events
    )


def bank(crew, assignment, live, at):
    """Record each landed message's turn evidence once; it is never undone."""
    if assignment["state"] == "done":
        return
    for message in assignment["messages"]:
        if (
            message.get("landed")
            and not message.get("activated")
            and started(crew, message, live, at)
        ):
            message["activated"] = True


def stalled(crew, assignment):
    """Whether a hook-delivered body reached the crew without starting a turn.

    A delivery hook stamps the receipt as it injects the body, which proves the text is in
    the crew's context and nothing more: injected context starts no turn. That receipt also
    empties drain's unread queue, so this is the only thing left that can tell a crew which
    never woke from one that is working, whatever its doorbell did. `drain` also runs from
    every captain command, where poll's banked flag may lag, so the event log decides.
    """
    if crew.record.get("provider") not in HOOK_DELIVERED or not assignment:
        return False
    return any(
        message.get("landed") and not message.get("activated") and not started(crew, message)
        for message in assignment["messages"]
    )


def drain_stamp(crew):
    return mail_dir(crew.session.directory, crew.crew_id) / ".drain"


def last_ring(crew):
    """An older session stamped a bare float; read it as a held ring for an unknown message."""
    stamp = drain_stamp(crew)
    if not stamp.exists():
        return None
    record = read_json(stamp)
    return record if isinstance(record, dict) else {"id": None, "at": record, "landed": False}


def landed_bodies(crew):
    """The messages whose body a ring landed in the pane.

    Only a provider outside HOOK_DELIVERED is rung with the body, and a ring repeated after
    landing goes content-free only because an earlier one carried it.
    """
    if crew.record.get("provider") in HOOK_DELIVERED:
        return set()
    record = last_ring(crew)
    if not record:
        return set()
    # A stamp written before bodies were carried names only its own landing.
    return set(record.get("bodies", ())) | ({record["id"]} if record["landed"] else set())


def drain(crew):
    """Retry a held doorbell; rate-limited so a polling loop never hammers it.

    Unread mail is the usual queue, but a hook-delivered body is stamped read before any
    turn runs, so a stalled crew's doorbell is retried on liveness instead.
    """
    if crew.is_background:
        return  # no doorbell: its owner reads the one message it runs
    messages = unread(crew)
    record = last_ring(crew)
    stall = False
    if messages:
        message_id = messages[0]["id"]
    elif record and stalled(crew, active_assignment(crew)):
        # Landed or held alike: a ring typed into a TUI still painting at launch is
        # swallowed, and that left a freshly recruited crew holding an unstarted
        # assignment with nothing but `tell` to save it.
        message_id, stall = record["id"], True
    else:
        return
    if record:
        # A ring that started no turn did not work, so it retries on the ordinary
        # cooldown; the slow one only exists to bounce a pane that died after the nudge.
        landed = record["landed"] and record["id"] == message_id and not stall
        if landed:
            interval = DRAIN_LANDED_INTERVAL
        elif record.get("gate") == UNREADABLE_GATE:
            # Recheck painting editors promptly, without bypassing their gate.
            interval = UNREADABLE_EXPIRY
        else:
            interval = DRAIN_INTERVAL
        registration_expired = (
            messages
            and record["id"] == message_id
            and record.get("gate") == "awaiting registration"
            and time.time() - messages[0]["queued_at"] >= REGISTRATION_GRACE
        )
        if not registration_expired and time.time() - record["at"] < interval:
            return
    ring(crew, message_id)


def deliver(
    crew,
    text,
    *,
    assignment_id=None,
    question_id=None,
    initial=False,
    message_id=None,
    queue_only=False,
):
    """Durably enqueue, then ring. queue_only leaves the ring to the caller, so recruit can
    queue before launch for the SessionStart hook and ring once the TUI is ready."""
    check_text(text, "message")
    if message_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
        raise CaptainError("[invalid-message-id] Use a bounded filename-safe message ID.")
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        kind = "assignment" if initial else "answer" if question_id else "followup"
        # Legacy callers have no retry token; derive one scoped to this exact request.
        message_id = (
            message_id
            or hashlib.sha256(
                json.dumps([assignment["id"], kind, question_id, text]).encode()
            ).hexdigest()
        )
        message = next((m for m in assignment["messages"] if m["id"] == message_id), None)
        if message:
            if any(
                message.get(key) != value
                for key, value in (("text", text), ("kind", kind), ("question_id", question_id))
            ):
                raise CaptainError("[message-id-conflict] Message ID has different content.")
        else:
            if assignment["state"] == "done":
                raise CaptainError("Assignment is done; use assign with an explicit handoff.")
            question = assignment["question"]
            if question and question_id != question["id"]:
                raise CaptainError(
                    "[question-id-required] Use answer with the pending question ID."
                )
            if question_id and (not question or question_id != question["id"]):
                raise CaptainError("[stale-question-id] Stale question ID.")
            message = {
                "id": message_id,
                "text": text,
                "kind": kind,
                "question_id": question_id,
                "delivery": "prepared",
            }
            assignment["messages"].append(message)
        assignment_id = assignment["id"]
    # Persist intent before mail: an interrupted enqueue can recover using the same ID.
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        message = next(m for m in assignment["messages"] if m["id"] == message_id)
        if message["delivery"] != "prepared":
            return message_id
        enqueue(crew, text, kind, assignment_id, message_id)
        message["delivery"] = "sent"
        mark_sent(assignment, message)
    if not queue_only:
        ring(crew, message_id)
    return message_id


def mark_sent(assignment, message):
    """A sent answer settles its question."""
    question = assignment["question"]
    if question and question["id"] == message["question_id"]:
        assignment["question"] = None
        assignment["state"] = "working"


def result(crew, assignment, status, summary="", delivery_id=None):
    return {
        "status": status,
        "delivery_id": delivery_id,
        "crew": crew.display_name,
        "assignment_id": assignment["id"] if assignment else None,
        "summary": summary,
    }


def prompt_summary(crew):
    """Never empty: the scrollback can be blank on a full-screen modal, so fall back to the
    visible screen, then to a pointer at the pane."""
    try:
        lines = crew.pane.lines()
    except HERDR_ERRORS as exc:
        # Only a transient read falls back; a gone pane or agent must still surface.
        if re.search(r"\b(pane_not_found|agent_not_found)\b|is not registered", str(exc)):
            raise
        lines = []
    if not any(line.strip() for line in lines):
        try:
            lines = crew.pane.lines(source="visible")
        except HERDR_ERRORS:
            lines = []
    body = "\n".join(lines).strip() or (
        f"(pane text unreadable; inspect pane {crew.record.get('pane') or crew.record['agent']})"
    )
    return "Native permission prompt (no policy verdict):\n" + body


def startup_attention(crew):
    """A native startup screen (Codex update or hook trust) as a notice, or None. It is
    startup attention for the user, never a tool approval and never a doorbell target."""
    screen = crew.pane.startup_screen()
    if not screen:
        return None
    pane = crew.record.get("pane") or crew.record["agent"]
    return (
        f"[startup-attention] {crew.display_name} is held at the {screen}: startup needs "
        f"attention, not approval. The user must answer it in pane {pane}; do not type into "
        "the pane or rerun the launcher. Queued mail stays queued until it clears."
    )


def herdr_status(crew):
    """One Herdr sample as (liveness, status, evidence), read by pane ID when recorded so a
    crew not yet registered by name still reads.

    liveness is alive, gone (Herdr names the pane or agent missing, or the pane runs no
    agent) or unknown; any other read error is unknown, never gone.
    """
    pane_id = crew.record.get("pane")
    try:
        if pane_id:
            pane = crew.pane.pane_record(pane_id)
            if not pane.get("agent"):
                return "gone", None, f"Herdr: no agent in pane {pane_id}"
            status = pane.get("agent_status")
        else:
            status = crew.pane.agent_status()
    except HERDR_ERRORS as exc:
        return ("gone" if GONE.search(str(exc)) else "unknown"), None, f"Herdr read failed: {exc}"
    return ("unknown" if status in (None, "unknown") else "alive"), status, f"Herdr: {status}"


def visible(crew):
    try:
        return crew.pane.screen(crew.record.get("pane"))
    except HERDR_ERRORS:
        return None


def unstarted(assignment, at=None):
    """Landed messages with no turn yet and no attention notice; past the window if at is given."""
    return [
        message
        for message in assignment["messages"]
        if message.get("landed")
        and not message.get("activated")
        and not message.get("attention")
        and (at is None or at - message["landed"]["at"] >= ACTIVATION_WINDOW)
    ]


def owes_attention(assignment, live, at):
    """Whether this tick may owe a needs_attention, so its screen is read before the lock."""
    if not assignment or assignment["state"] == "done" or assignment.get("attention"):
        return False
    return live == "blocked" or bool(unstarted(assignment, at))


def attend(crew, assignment, sample, at, screen):
    """The needs_attention or crew-gone notice this tick owes, else None; each is owed once.

    Herdr's blocked opens one episode, which only a successful non-blocked read ends. Landed
    mail that started no turn within ACTIVATION_WINDOW is the provider-neutral fallback for
    a screen no recognizer knows and Herdr may never call blocked.
    """
    liveness, live, herdr = sample
    if (
        liveness == "gone"
        and not assignment.get("gone")
        and any(message.get("activated") for message in assignment["messages"])
    ):
        assignment["gone"] = herdr
        return (
            "error",
            f"[crew-gone] {crew.display_name}'s native agent is gone ({herdr}) after it "
            "started on its mail, with the assignment unfinished. Check its owned paths, "
            "then dismiss it and recruit a replacement.",
        )
    if assignment.get("attention"):
        return None
    waiting, stuck = unstarted(assignment), unstarted(assignment, at)
    if live != "blocked" and not stuck:
        return None
    for message in waiting:
        message["attention"] = True
    if live == "blocked":
        assignment["attention"] = {"at": at, "herdr": herdr}
        # A hook approval, or a held ring's startup-screen notice, already reported it.
        if assignment.get("blocked_notice") is not None or (
            startup_screen_name(screen or [])
            and (assignment.get("hooks_untrusted") or assignment.get("startup_update"))
        ):
            return None
        reason = "is blocked"
    else:
        reason = (
            f"has started no turn {round(at - stuck[0]['landed']['at'])}s after its mail landed"
        )
    pane = crew.record.get("pane") or crew.record["agent"]
    text = "\n".join(screen[-ATTENTION_LINES:]) if screen else "(unreadable)"
    return (
        "needs_attention",
        f"[needs-attention] {crew.display_name} {reason} ({herdr}). Ask the user to look at "
        f"pane {pane}; do not type into it.\nVisible screen:\n{text}",
    )


def modal_summary(crew, assignment):
    """Guard both modal reads; an unreadable pane is otherwise conservatively a modal."""
    try:
        if crew.pane.choice_modal():
            return prompt_summary(crew)
    except HERDR_ERRORS as exc:
        messages = unread(crew)
        if not messages or not awaiting_registration(
            crew, messages[0]["id"], last_ring(crew), exc, assignment
        ):
            raise
    return None


def ends_block(sample):
    """Only a successful non-blocked read ends a block; a failed read never does."""
    return sample[0] == "alive" and sample[1] != "blocked"


def screen_due(crew, assignment, ack):
    """Whether this poll reaches the screen heuristics: no delivery, queued notice or native
    approval event is ahead of them, so a pane that cannot be read is never read for nothing."""
    pending = assignment["pending"]
    if pending and pending["result"]["delivery_id"] != ack:
        return False
    seq = pending["seq"] if pending else assignment["ack_seq"]
    if assignment["state"] == "done" or seq < len(assignment["notices"]):
        return False
    offset = pending["offset"] if pending else assignment.get("scan_offset", assignment["offset"])
    events, _ = read_events(crew.events, offset, max_bytes=MAX_EVENT_BYTES)
    return not any(event_status(event) == "blocked" for event in events)


def heuristics(crew, assignment):
    """The throttled screen reads for modals Herdr calls idle: a Codex startup screen, else
    a choice modal's summary. A read error is returned, to raise where it is used."""
    if crew.record.get("provider") == "codex" and (startup := startup_attention(crew)):
        return startup, None
    try:
        return None, modal_summary(crew, assignment)
    except HERDR_ERRORS as exc:
        return None, exc


def poll(crew, ack=None, modal=True):
    drain(crew)
    paneless = crew.is_background
    # Every Herdr read happens here, before the lock: a slow read must not hold up other
    # protocol verbs. Status is read every tick; the screen heuristics are throttled and
    # skipped for an agent Herdr confirms gone or a block the captain already has.
    sample, at, screen, looked = (None, None, None), time.time(), None, None
    peek = None if paneless else active_assignment(crew)
    if not paneless:
        sample = herdr_status(crew)
        at = time.time()
        if owes_attention(peek, sample[1], at):
            screen = visible(crew)
        if (
            modal
            and peek
            and sample[0] != "gone"
            and crew.record.get("provider") in MODAL_PROVIDERS
            and (ends_block(sample) or not peek.get("attention"))
            and screen_due(crew, peek, ack)
        ):
            looked = peek["id"], *heuristics(crew, peek)
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew)
        if not peek or (peek["id"], peek["incarnation_id"]) != (
            assignment["id"],
            assignment["incarnation_id"],
        ):
            # A handoff while Herdr was read: that evidence belongs to the old assignment.
            sample, screen, looked = (None, None, None), None, None
        if not paneless:
            bank(crew, assignment, sample[1], at)
            if ends_block(sample):
                # Even behind an unacknowledged delivery, or the next block is never news.
                assignment.pop("blocked_notice", None)
                assignment.pop("attention", None)
        pending, acked = assignment["pending"], False
        if ack:
            if pending and ack == pending["result"]["delivery_id"]:
                assignment["ack_seq"] = pending["seq"]
                assignment["offset"] = pending["offset"]
                assignment["scan_offset"] = pending["offset"]
                assignment["native_status"] = pending["native_status"]
                assignment["pending"] = None
                assignment["last_ack"] = ack
                pending, acked = None, True
            elif ack != assignment["last_ack"]:
                raise CaptainError("[stale-ack] Unknown delivery acknowledgement.")
        question = assignment["question"]
        if question:
            question.setdefault("due_at", time.time() + QUESTION_INTERVAL)
            if time.time() >= question["due_at"] and not question.get("escalated"):
                reasked = question.get("reasked", False)
                code = "question-escalated" if reasked else "question-reasked"
                notice(
                    assignment,
                    "error" if reasked else "asked",
                    f"[{code}] {question['id']}: {question['text']}",
                )
                question["escalated" if reasked else "reasked"] = True
                question["due_at"] = time.time() + QUESTION_INTERVAL
        if pending:
            if pending["result"]["status"] == "done" and assignment["report"]:
                pending["result"]["summary"] = done_summary(assignment)
            return pending["result"]
        if paneless:
            reconcile_background(crew, assignment)
        seq, offset = assignment["ack_seq"], assignment.get("scan_offset", assignment["offset"])
        native_status, cleared, blocked_id = assignment["native_status"], False, ""
        if seq < len(assignment["notices"]):
            entry = assignment["notices"][seq]
            seq += 1
            status, summary = entry["status"], entry["summary"]
            if status == "done" and assignment["report"]:
                summary = done_summary(assignment)
        elif assignment["state"] == "done":
            if acked:
                return result(crew, assignment, "idle")
            raise CaptainError(
                "Assignment is done and fully acknowledged; dismiss the crew or hand off."
            )
        else:
            events, offset = read_events(crew.events, offset, max_bytes=MAX_EVENT_BYTES)
            if crew.record.get("provider") == "codex":
                hooks_live(crew, assignment, events)
            held = assignment.get("blocked_notice")
            if held is not None and any(
                e.get("hook_event_name") in ("PostToolUse", "PermissionDecision")
                and (not held or e.get("tool_use_id") == held)
                for e in events
            ):
                assignment.pop("blocked_notice")
            # Native inactivity (Stop, turn complete) is evidence, never a delivery.
            notified = [(i, e) for i, e in enumerate(events) if event_status(e) == "blocked"]
            last = notified[-1][0] if notified else len(events)
            answered = [
                i
                for i, e in enumerate(events)
                if (s := event_status(e)) in ("done", "idle")
                or (e.get("hook_event_name") in TOOL_EVENTS and s != "blocked")
            ]
            # A later tool call or turn end proves the earlier approval was answered.
            if any(i < last for i in answered):
                native_status = assignment["native_status"] = None
            cleared = any(i > last for i in answered)
            if notified:
                status, event = "awaiting_approval", notified[-1][1]
                # A parallel tool event can follow a still-open escalation; Herdr knows.
                if cleared and native_status != status and sample[1] == "blocked":
                    cleared = False
                # One approval arrives twice, as a PermissionRequest hook and a
                # permission_prompt Notification, usually in separate polls.
                new_ask = any(e.get("hook_event_name") == "PreToolUse" for _, e in notified)
                if cleared or (native_status == status and not new_ask):
                    assignment["scan_offset"] = offset
                    if cleared:
                        assignment["native_status"] = None
                    return result(crew, assignment, "working")
                detail = next((e for _, e in reversed(notified) if e.get("tool_input")), event)
                blocked_id = str(detail.get("tool_use_id") or "")
                inputs = detail.get("tool_input")
                command = inputs.get("command") if isinstance(inputs, dict) else None
                command = command or detail.get("message") or json.dumps(inputs, ensure_ascii=False)
                approval = detail.get("approval") or {}
                code = approval.get("code")
                reason = REASONS[code][1] if code in REASONS else "native permission prompt"
                summary = f"{detail.get('tool_name', 'Approval')} [{code or 'native'}]: {reason}\n{command}"
                native_status = status
            else:
                failed = (
                    not crew.record.get("pane") and crew.record.get("status") == "needs_attention"
                )
                attention = (
                    None
                    if paneless or failed or sample[0] is None
                    else attend(crew, assignment, sample, at, screen)
                )
                # The screen read before the lock counts only for this assignment, and not
                # once an attention episode has shown the captain this screen.
                read_pane = (
                    looked is not None
                    and looked[0] == assignment["id"]
                    and not assignment.get("attention")
                )
                if failed:
                    status, summary = (
                        "error",
                        "Crew startup failed; inspect before explicit done/handoff.",
                    )
                elif attention:
                    # A notice, so its Herdr evidence outlives the acknowledgement. One this
                    # poll queued first (hooks_live) is the older, so it is delivered first.
                    notice(assignment, *attention)
                    entry = assignment["notices"][seq]
                    status, summary = entry["status"], entry["summary"]
                    seq += 1
                elif read_pane and looked[1]:
                    # A ring held at this screen already posted its own notice.
                    held = assignment.get("hooks_untrusted") or assignment.get("startup_update")
                    status = None if held else "error"
                    summary = looked[1]
                elif read_pane and looked[2] is not None:
                    if isinstance(looked[2], Exception):
                        raise looked[2]
                    status, summary = "awaiting_approval", looked[2]
                else:
                    status = None
                if not attention and (status is None or status == native_status):
                    assignment["scan_offset"] = offset
                    if status is None and (read_pane or ends_block(sample)):
                        # Only a pane we actually looked at clears the dedupe. Clearing on
                        # the absence of a read wiped it every poll for event-driven
                        # providers, which is how one approval surfaced once per event.
                        assignment["native_status"] = None
                    return result(crew, assignment, "working")
                if not attention:
                    native_status = status
        if status == "awaiting_approval":
            assignment["blocked_notice"] = blocked_id
        generation = assignment.get("generation", 0) + 1
        assignment["generation"] = generation
        delivery_id = f"{assignment['id']}:{assignment['incarnation_id']}:{generation}"
        response = result(crew, assignment, status, summary, delivery_id)
        assignment["pending"] = {
            "result": response,
            "seq": seq,
            "offset": offset,
            "native_status": None if cleared else native_status,
        }
        return response


def wait_ticks(timeout):
    """Poll at least once, throttling modal reads and sleeping only until the deadline."""
    deadline = time.monotonic() + timeout
    looked = None
    while True:
        now = time.monotonic()
        modal = looked is None or now - looked >= MODAL_INTERVAL
        if modal:
            looked = now
        yield modal
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1, remaining))


def wait_all(current, timeout):
    """Return every crew's pending delivery without advancing any acknowledgement."""

    for modal in wait_ticks(timeout):
        current = current._replace(meta=read_json(current.meta_path))
        deliveries = []
        for crew in sorted(Crew.members(current), key=lambda crew: crew.crew_id):
            assignment = active_assignment(crew)
            if (
                crew.is_dismissed
                or not assignment
                or assignment["incarnation_id"] != crew.record.get("incarnation_id")
                or (
                    assignment["state"] == "done"
                    and not assignment["pending"]
                    and assignment["ack_seq"] == len(assignment["notices"])
                )
            ):
                continue
            try:
                response = poll(crew, modal=modal)
            except CaptainError as exc:
                deliveries.append(result(crew, assignment, "error", str(exc)))
                continue
            if response["delivery_id"]:
                deliveries.append(response)
        if deliveries:
            return {"status": "ready", "deliveries": deliveries}
    return {"status": "timeout", "deliveries": []}


def wait(crew, timeout, ack=None):
    """Poll native signals and Herdr status every tick; only screen heuristics are throttled.

    A wait that reaches its deadline with nothing to report returns `timeout`, and a
    captain reading that is a model turn spent to learn nothing. `timeout` therefore
    exists only for a caller that asked for a bounded look (`--timeout`, including 0 for
    "report what has already arrived"); the configured default is a full day, long enough
    that quiet never wakes anyone. This is the no-daemon equivalent of a watcher process:
    the captain already backgrounds this, so keeping it quiet costs nothing, while
    returning early costs a whole context.
    """
    for modal in wait_ticks(timeout):
        response = poll(crew, ack, modal)
        ack = None
        if response["delivery_id"] or response["status"] == "idle":
            return response
    response["status"] = "timeout"
    return response


def close_unreported(assignment, outcome, summary):
    """End an assignment that produced no report: visibly an error, never a done."""
    notice(assignment, "error", summary)
    assignment.update(state="done", outcome=outcome, report=summary)


def reconcile_background(crew, assignment):
    """Close an unfinished assignment whose owner is gone; the caller holds the checkpoint."""
    if assignment["state"] == "done" or background_state.alive(crew):
        return
    if background_state.paths(background_state.stem(crew))["abandoned"].exists():
        close_unreported(
            assignment, "failed", "[background-start] The run was withdrawn before it started."
        )
        return
    close_unreported(
        assignment,
        "lost",
        "[background-lost] The owner process ended without recording an outcome; "
        f"nothing was restarted. Evidence: {background_state.stem(crew).parent}",
    )
