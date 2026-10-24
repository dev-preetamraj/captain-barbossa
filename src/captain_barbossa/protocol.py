"""Explicit assignment state and acknowledged delivery, local to one session."""

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import lifetime
from .models import HOOK_DELIVERED, HOOKLESS
from .pane import DRAFT_GATE, HOLD_EXPIRY, UNREADABLE_EXPIRY, UNREADABLE_GATE
from .runtime import HERDR_ERRORS, CaptainError, check_text, inside_project
from .sessions import session_directories
from .store import lock, read_json, state_root, state_storage, temp_root, write_json

ACTIONS = ("read", "search", "edit", "test", "build", "format", "commit", "version", "delete")
WRITE_ACTIONS = ("edit", "format", "commit", "version", "delete")
# Our own ring is still painting for about this long, so a draft seen inside it is ours.
ECHO_GRACE = 2
# Native "working" is a wake, not news: it fires at recruit and at every crew turn end.
NOTIFIED = {"done": "idle", "idle": "idle", "blocked": "awaiting_approval"}
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PermissionDecision")
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
# A hook-delivered body lands in the crew's context without starting a turn, and the receipt
# the hook stamps then empties drain's queue, so nothing else ever reports this crew.
STALLED_SUMMARY = (
    "Mail was handed over but no turn has ever started; the doorbell is being retried, "
    "so read the pane before sending anything else."
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


def active(state, crew, assignment_id=None, incarnation=None):
    key = state["active"].get(crew.crew_id)
    assignment = state["assignments"].get(key)
    if not assignment or not crew.record.get("incarnation_id"):
        raise CaptainError("Legacy crew has no protocol assignment; recruit new crew explicitly.")
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


def begin(crew, project, task, paths=(), actions=(), handoff=None, *, assignment_id=None):
    if not isinstance(task, str) or not task.strip():
        raise CaptainError("[empty-goal] Assignment goal must not be empty.")
    check_text(task, "task")
    if PLACEHOLDER.search(task) or any(PATH_PLACEHOLDER.search(str(path)) for path in paths):
        raise CaptainError("[placeholder] Replace assignment placeholder text.")
    # execution imports protocol for assignment lookup.
    from .execution import root

    execution_root = root(crew.session, crew.record)
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
    if not crew.record.get("incarnation_id"):
        raise CaptainError("Legacy crew cannot be silently adopted; recruit new crew.")
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


def check_action(assignment, project, action, paths):
    if assignment["state"] == "done":
        raise CaptainError("Assignment is already done.")
    if action not in assignment["actions"]:
        raise CaptainError(f"[action-not-granted] Action {action!r} was not granted.")
    if action in WRITE_ACTIONS:
        if not paths:
            raise CaptainError("This action requires explicit owned paths.")
        for path in owned_paths(project, paths):
            if not any(Path(path).is_relative_to(Path(owner)) for owner in assignment["paths"]):
                raise CaptainError(f"[outside-grant] Path is not owned by this assignment: {path}")


def _git(project, *args):
    """Read-only git in the project, or None when there is no readable work tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result


def unlanded(project, assignment):
    """Reason codes for owned work Git shows as not landed; empty when Git shows none.

    A report is evidence, not proof: every commit it names must be reachable from a ref,
    and the owned paths must hold no uncommitted or untracked change.
    """
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
        if changed:
            reasons.append(f"[uncommitted] {', '.join(sorted(changed))}")
    missing = []
    for token in dict.fromkeys(COMMIT_TOKEN.findall(assignment.get("report") or "")):
        found = _git(project, "rev-parse", "--verify", "--quiet", f"{token}^{{commit}}")
        refs = (
            _git(project, "for-each-ref", "--count=1", "--contains", found.stdout.strip())
            if found is not None and not found.returncode
            else None
        )
        if refs is None or refs.returncode or not refs.stdout.strip():
            missing.append(token)
    if missing:
        reasons.append(f"[unreachable-commit] {', '.join(missing)}")
    return reasons


def change(crew, args, project):
    assignment_id, incarnation = validate_actor(crew, args)
    if args.command == "done":
        # `done` is the crew's last action, so it runs before the Stop hook and a refusal
        # here pre-empts that delivery. Hand the mail over rather than naming a command.
        deliver_before_done(crew)
    with checkpoint(crew.session.directory) as state:
        # `ask` binds to the live assignment, not the launch-bound id: it is the crew's
        # only way out, and a stale id used to block it. Everything else keeps the explicit
        # id, so a report never attaches to an assignment the captain replaced.
        assignment = active(
            state, crew, None if args.command == "ask" else assignment_id, incarnation
        )
        if args.command == "check":
            # execution imports protocol for assignment lookup.
            from .execution import root

            if lifetime.revoked(crew.session, crew.record):
                raise CaptainError("[action-not-granted] Crew lifetime was revoked.")
            check_action(assignment, root(crew.session, crew.record), args.action, args.path)
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
        check_text(args.report, "report")
        if assignment["question"]:
            raise CaptainError("Answer the pending question before done.")
        if any(message["delivery"] == "prepared" for message in assignment["messages"]):
            raise CaptainError("[delivery-pending] Retry the prepared message before done.")
        if unread(crew):
            # Raced past deliver_before_done; the retry hands it over.
            raise CaptainError("Mail arrived while this report was being filed; retry done.")
        assignment["report"] = args.report
        assignment["state"] = "done"
        notice(assignment, "done", args.report)
        return {"assignment_id": assignment["id"], "status": "done"}


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


def receipt(session_directory, crew_id, ids):
    """The one writer of `read`, for the crew's own `inbox` and for a delivery hook."""
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
        and not assignment.get("turn_started")
        and 0 <= time.time() - message["queued_at"] < REGISTRATION_GRACE
        and not any(
            turn_event(event)
            for event in native_events(crew.events, assignment.get("offset", 0))[0]
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
    if unreachable is not None:
        bounce(crew, message_id, unreachable)


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


def stalled(crew, assignment):
    """Whether a hook-delivered body reached the crew without ever starting a turn.

    A delivery hook stamps the receipt as it injects the body, which proves the text is in
    the crew's context and nothing more: injected context starts no turn. That receipt also
    empties drain's unread queue, so this is the only thing left that can tell a crew which
    never woke from one that is working, whatever its doorbell did.
    """
    if crew.record.get("provider") not in HOOK_DELIVERED:
        return False
    if not assignment or assignment.get("turn_started"):
        return False
    messages = assignment["messages"]
    if not messages or any(message["delivery"] != "read" for message in messages):
        return False
    # turn_started is written by `poll` alone, while `drain` also runs from every captain
    # command, so the flag alone called a crew that had long since woken and emptied its
    # queue stalled, and rang it once a command for the rest of the session. The crew's own
    # event file is the same proof poll uses; read from the assignment's baseline and not
    # consumed, so a turn poll has already banked stays visible here.
    events, _ = native_events(crew.events, assignment.get("offset", 0))
    return not any(turn_event(event) for event in events)


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
    """A delivered message re-arms the single native idle notification it may produce."""
    assignment["idle_seen"] = False
    question = assignment["question"]
    if question and question["id"] == message["question_id"]:
        assignment["question"] = None
        assignment["state"] = "working"


def native_events(path, offset):
    """Bound each native-log read; leave partial lines for the next poll."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(65536)
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    if not end and len(data) == 65536:
        raise CaptainError("Native event exceeds 64 KiB; inspect the event file.")
    events = []
    for line in data[:end].splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, offset + end


def result(crew, assignment, status, summary="", delivery_id=None):
    return {
        "status": status,
        "delivery_id": delivery_id,
        "crew": crew.display_name,
        "assignment_id": assignment["id"] if assignment else None,
        "summary": summary,
    }


def modal_summary(crew, assignment):
    """Guard both modal reads; an unreadable pane is otherwise conservatively a modal."""
    try:
        if crew.pane.choice_modal():
            return "Native permission prompt (no policy verdict):\n" + "\n".join(crew.pane.lines())
    except HERDR_ERRORS as exc:
        messages = unread(crew)
        if not messages or not awaiting_registration(
            crew, messages[0]["id"], last_ring(crew), exc, assignment
        ):
            raise
    return None


def poll(crew, ack=None, modal=True):
    from .approval import REASONS
    from .crew import event_status

    drain(crew)
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew)
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
                pending["result"]["summary"] = assignment["report"]
            return pending["result"]
        paneless = crew.is_background
        if paneless:
            from .background import reconcile

            reconcile(crew, assignment)
        seq, offset = assignment["ack_seq"], assignment.get("scan_offset", assignment["offset"])
        native_status, cleared = assignment["native_status"], False
        if seq < len(assignment["notices"]):
            entry = assignment["notices"][seq]
            seq += 1
            status, summary = entry["status"], entry["summary"]
            if status == "done" and assignment["report"]:
                summary = assignment["report"]
        elif assignment["state"] == "done":
            if acked:
                return result(crew, assignment, "idle")
            raise CaptainError(
                "Assignment is done and fully acknowledged; dismiss the crew or hand off."
            )
        else:
            events, offset = native_events(crew.events, offset)
            if any(turn_event(event) for event in events):
                # Liveness is a started turn, never a delivered body; this is committed
                # outside the ack dance because it only ever goes one way.
                assignment["turn_started"] = True
            notified = [
                (i, NOTIFIED[s], e)
                for i, e in enumerate(events)
                if (s := event_status(e)) in NOTIFIED
            ]
            last = notified[-1][0] if notified else len(events)
            tools = [
                i
                for i, e in enumerate(events)
                if e.get("hook_event_name") in TOOL_EVENTS and event_status(e) != "blocked"
            ]
            # A later tool call proves the earlier approval was answered.
            if any(i < last for i in tools):
                native_status = assignment["native_status"] = None
            cleared = any(i > last for i in tools)
            if notified:
                _, status, event = notified[-1]
                summary = str(event.get("message") or event.get("last-assistant-message") or "")
                if status == "awaiting_approval":
                    # One approval arrives twice, as a PermissionRequest hook and a
                    # permission_prompt Notification, usually in separate polls.
                    new_ask = any(e.get("hook_event_name") == "PreToolUse" for _, _, e in notified)
                    if native_status == status and not new_ask:
                        assignment["scan_offset"] = offset
                        if cleared:
                            assignment["native_status"] = None
                        return result(crew, assignment, "working")
                    detail = next(
                        (
                            e
                            for _, s, e in reversed(notified)
                            if s == status and e.get("tool_input")
                        ),
                        event,
                    )
                    inputs = detail.get("tool_input")
                    command = inputs.get("command") if isinstance(inputs, dict) else None
                    command = (
                        command or detail.get("message") or json.dumps(inputs, ensure_ascii=False)
                    )
                    approval = detail.get("approval") or {}
                    code = approval.get("code")
                    reason = REASONS[code][1] if code in REASONS else "native permission prompt"
                    summary = f"{detail.get('tool_name', 'Approval')} [{code or 'native'}]: {reason}\n{command}"
                    native_status = status
                elif assignment.get("idle_seen"):
                    assignment["scan_offset"] = offset
                    # A turn that ran proves the approval cleared.
                    assignment["native_status"] = None
                    return result(crew, assignment, "working")
                else:
                    # One idle per delivered message; every later crew turn also ends idle.
                    assignment["idle_seen"] = True
                    native_status = None
            else:
                # Live state rather than a consumed event, so native_status is its only dedupe.
                read_pane = (
                    modal and not paneless and crew.record.get("provider") in MODAL_PROVIDERS
                )
                if not crew.record.get("pane") and crew.record.get("status") == "needs_attention":
                    status, summary = (
                        "error",
                        "Crew startup failed; inspect before explicit done/handoff.",
                    )
                elif read_pane and (summary := modal_summary(crew, assignment)) is not None:
                    status = "awaiting_approval"
                elif not paneless and stalled(crew, assignment):
                    status, summary = "error", STALLED_SUMMARY
                else:
                    status = None
                if status is None or status == native_status:
                    assignment["scan_offset"] = offset
                    if status is None and read_pane:
                        # Only a pane we actually looked at clears the dedupe. Clearing on
                        # the absence of a read wiped it every poll for event-driven
                        # providers, which is how one approval surfaced once per event.
                        assignment["native_status"] = None
                    return result(crew, assignment, "working")
                native_status = status
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


def wait_all(current, timeout):
    """Return every crew's pending delivery without advancing any acknowledgement."""
    from .crew import Crew

    deadline = time.monotonic() + timeout
    looked = None
    while True:
        now = time.monotonic()
        modal = looked is None or now - looked >= MODAL_INTERVAL
        if modal:
            looked = now
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "deliveries": []}
        time.sleep(min(1, remaining))


def wait(crew, timeout, ack=None):
    """Poll on native signals; the one pane read a hookless provider needs is throttled.

    A wait that reaches its deadline with nothing to report returns `timeout`, and a
    captain reading that is a model turn spent to learn nothing. `timeout` therefore
    exists only for a caller that asked for a bounded look (`--timeout`, including 0 for
    "report what has already arrived"); the configured default is a full day, long enough
    that quiet never wakes anyone. This is the no-daemon equivalent of a watcher process:
    the captain already backgrounds this, so keeping it quiet costs nothing, while
    returning early costs a whole context.
    """
    deadline = time.monotonic() + timeout
    looked = None
    while True:
        now = time.monotonic()
        modal = looked is None or now - looked >= MODAL_INTERVAL
        if modal:
            looked = now
        response = poll(crew, ack, modal)
        ack = None
        if response["delivery_id"] or response["status"] == "idle":
            return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            response["status"] = "timeout"
            return response
        time.sleep(min(1, remaining))
