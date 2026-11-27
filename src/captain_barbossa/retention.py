"""Session retention: prune old session directories with no live agent or unresolved work."""

import shutil
import time

from . import lifetime, native_history, results, worktrees
from .runtime import HERDR_ERRORS, CaptainError, herdr
from .sessions import (
    SESSION_ID,
    Session,
    crew_prefix,
    newest_mtime,
    session_directories,
    set_archived,
)
from .store import lock, read_json, state_root, temp_root

PRUNE_DAYS = 7
FORCEABLE_BLOCKERS = {"question", "report", "mail", "delivery", "result"}


class SessionDeleteError(CaptainError):
    """A delete refusal with blockers the caller can present or override."""

    def __init__(self, message, blockers):
        super().__init__(message)
        self.blockers = blockers


def _blocker(kind, summary, *, crew=None, assignment_id=None):
    return {
        "kind": kind,
        "crew": crew,
        "assignment_id": assignment_id,
        "summary": " ".join(str(summary).split()),
    }


def live_agents():
    """Terminal IDs and agent names Herdr reports, or None when Herdr cannot be reached."""
    try:
        agents = herdr("agent", "list", timeout=10).get("agents") or []
    except HERDR_ERRORS:
        return None
    return (
        {agent.get("terminal_id") for agent in agents if agent.get("terminal_id")},
        {agent.get("name") for agent in agents if agent.get("name")},
    )


def holds_live_agent(directory, agents):
    """Whether the session's captain terminal or a crew agent is in `agents` from live_agents."""
    terminal_ids, names = agents
    terminal_id = session_terminal_id(directory)
    prefix = crew_prefix(directory.name)
    return bool(terminal_id and terminal_id in terminal_ids) or any(
        name.startswith(prefix) for name in names
    )


def session_terminal_id(directory):
    """The captain's recorded Herdr terminal ID, or None if never recorded.

    Pane IDs are position-in-layout and Herdr recycles them across terminals, so
    liveness cannot key on the pane; the terminal ID is stable for the process.
    """
    path = directory / "captain.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except CaptainError:
        return None
    terminal_id = data.get("terminal_id") if isinstance(data, dict) else None
    return terminal_id if isinstance(terminal_id, str) else None


def _assignment_for(state, crew_id, incarnation_id=None):
    for assignment in state.get("assignments", {}).values():
        if assignment.get("crew", "").casefold() != str(crew_id).casefold():
            continue
        if incarnation_id is None or assignment.get("incarnation_id") == incarnation_id:
            return assignment.get("id")
    return None


def _protocol_blockers(current, state):
    blockers = []
    roster = current.meta.get("crew", {})
    for crew_id, assignment_id in state.get("active", {}).items():
        assignment = state.get("assignments", {}).get(assignment_id)
        if not assignment:
            continue
        record = roster.get(crew_id)
        if record is not None and (
            record.get("status") == "dismissed"
            or assignment.get("incarnation_id") != record.get("incarnation_id")
        ):
            continue
        question = assignment.get("question")
        if question:
            blockers.append(
                _blocker(
                    "question",
                    f"A question from {crew_id} is still waiting for an answer: "
                    f"{question.get('text', 'question text unavailable')}",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
        mail_blocked = False
        folder = current.directory / "mail" / crew_id
        try:
            messages = [read_json(path) for path in sorted(folder.glob("*.json"))]
        except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
            messages = []
            blockers.append(
                _blocker(
                    "mail",
                    f"Mail for {crew_id} could not be read safely.",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
            mail_blocked = True
        for message in messages:
            if message.get("assignment_id") != assignment_id:
                continue
            if message.get("state") == "queued":
                summary = f"Mail to {crew_id} has not been read."
            elif message.get("state") == "bounced":
                reason = message.get("reason") or "the crew could not receive it"
                summary = f"Mail to {crew_id} bounced because {reason}."
            else:
                continue
            blockers.append(_blocker("mail", summary, crew=crew_id, assignment_id=assignment_id))
            mail_blocked = True
        prepared = any(
            message.get("delivery") == "prepared" for message in assignment.get("messages", [])
        )
        unreceived = bool(assignment.get("pending")) or assignment.get("ack_seq", 0) < len(
            assignment.get("notices", [])
        )
        if assignment.get("state") == "done" and assignment.get("report") and unreceived:
            blockers.append(
                _blocker(
                    "report",
                    f"A report from {crew_id} has not been acknowledged.",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
        elif unreceived and not question:
            blockers.append(
                _blocker(
                    "delivery",
                    f"A notification from {crew_id} has not been acknowledged.",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
        if prepared:
            blockers.append(
                _blocker(
                    "delivery",
                    f"A message to {crew_id} was prepared but not sent.",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
        if assignment.get("state") != "done" and not question and not mail_blocked and not prepared:
            blockers.append(
                _blocker(
                    "delivery",
                    f"The assignment for {crew_id} is unfinished.",
                    crew=crew_id,
                    assignment_id=assignment_id,
                )
            )
    return blockers


def session_blockers(directory):
    """Return every retained work, outcome and cleanup blocker for a session."""
    try:
        meta = read_json(directory / "session.json")
        current = Session(directory, meta)
        path = directory / "protocol.json"
        state = read_json(path) if path.exists() else {"active": {}, "assignments": {}}
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        return [_blocker("delivery", "The session state could not be read safely.")]
    blockers = _protocol_blockers(current, state)
    try:
        records = worktrees.inventory(current)
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        records = []
        blockers.append(_blocker("worktree", "Crew worktree state could not be read safely."))
    for record in records:
        if record.get("phase") == "removed":
            continue
        blockers.append(
            _blocker(
                "worktree",
                f"worktree {record.get('id')} for {record.get('crew_id')} has not been removed.",
                crew=record.get("crew_id"),
                assignment_id=record.get("assignment_id"),
            )
        )
    record_ids = {record.get("id") for record in records}
    try:
        manifests = results.inventory(current)
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        manifests = []
        blockers.append(_blocker("result", "Crew result state could not be read safely."))
    reviewed = {
        result_id
        for assignment in state.get("assignments", {}).values()
        for result_id in (assignment.get("reviewed_results") or {})
    }
    for manifest in manifests:
        folder = current.directory / "results" / manifest["id"]
        try:
            receipt = folder / "transfer.json"
            applied = receipt.is_file() and read_json(receipt).get("state") == "applied"
            settled = manifest["id"] in reviewed or applied or (folder / "discard.json").exists()
        except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
            settled = False
        if settled and manifest.get("worktree_id") in record_ids:
            continue
        summary = (
            f"result {manifest.get('id')} has no worktree cleanup record."
            if manifest.get("worktree_id") not in record_ids
            else f"result {manifest.get('id')} has not been applied, discarded or reviewed."
        )
        blockers.append(
            _blocker(
                "result",
                summary,
                crew=manifest.get("crew_id"),
                assignment_id=manifest.get("assignment_id"),
            )
        )
    lifetimes = directory / "lifetimes"
    try:
        for path in sorted(lifetimes.glob("*.json")):
            record = lifetime.read(current, path.stem)
            verdict = lifetime.check(
                current,
                path.stem,
                crew_id=record["crew_id"],
                incarnation_id=record["incarnation_id"],
                root=record["root"],
                worktree=record["worktree_id"],
            )
            if verdict.ok:
                continue
            blockers.append(
                _blocker(
                    "lifetime",
                    f"The managed writer for {record['crew_id']} is not confirmed stopped: "
                    f"{verdict.reason}.",
                    crew=record["crew_id"],
                    assignment_id=_assignment_for(
                        state, record["crew_id"], record.get("incarnation_id")
                    ),
                )
            )
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        blockers.append(_blocker("lifetime", "Managed writer state could not be read safely."))
    history = directory / "native-history"
    history_blocked = False
    try:
        for path in sorted(history.glob("*.json")):
            record = read_json(path)
            if record.get("state") == "complete":
                continue
            crew_id = record.get("crew_id")
            reason = record.get("reason") or "cleanup is incomplete"
            blockers.append(
                _blocker(
                    "history",
                    f"Native history cleanup for {crew_id or 'a crew member'} is incomplete: "
                    f"{reason}.",
                    crew=crew_id,
                    assignment_id=_assignment_for(state, crew_id, record.get("incarnation_id")),
                )
            )
            history_blocked = True
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        blockers.append(_blocker("history", "Native history state could not be read safely."))
        history_blocked = True
    if native_history.pending(directory) and not history_blocked:
        blockers.append(_blocker("history", "Native history cleanup is incomplete."))
    return blockers


def unresolved(directory):
    """Whether a session holds work or an outcome nobody has received yet."""
    try:
        meta = read_json(directory / "session.json")
        if meta.get("transport") == "supervisor":
            from . import supervision  # local: Herdr sessions never load the supervisor door

            if supervision.alive(directory):
                return True
        return bool(session_blockers(directory))
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        return True


def prune_sessions(project, days=PRUNE_DAYS, current=None):
    """Remove session directories older than `days` that hold no live Herdr agent and no
    unresolved work, in both the durable and the legacy temp root.

    Herdr being unreachable makes liveness unknowable, so the cutoff doubles rather
    than guessing. Returns the directories removed.
    """
    directories = [path for path in session_directories(project) if path.name != current]
    if not directories:
        return []
    agents = live_agents()
    cutoff = time.time() - days * 86400 * (2 if agents is None else 1)
    removed = []
    for directory in directories:
        if newest_mtime(directory) >= time.time() - days * 86400:
            continue
        with lock(directory / "session.lock"):
            activity = newest_mtime(directory)
            if activity >= cutoff or unresolved(directory):
                continue
            if agents is not None and holds_live_agent(directory, agents):
                continue
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append(directory)
    return removed


def check_stopped(directory):
    """Refuse with a typed code while a supervisor holds supervisor.lock or Herdr agents run.

    An unreachable Herdr proves no agent, so only a listed agent refuses a Herdr session.
    """
    meta = read_json(directory / "session.json")
    if meta.get("transport") == "supervisor":
        from . import supervision  # local: Herdr sessions never load the supervisor door

        if supervision.alive(directory):
            raise CaptainError(
                f"[supervisor-running] A supervisor runs session {directory.name}; stop it first."
            )
        return
    agents = live_agents()
    if agents is not None and holds_live_agent(directory, agents):
        raise CaptainError(
            f"[session-live-agents] Session {directory.name} has live Herdr agents; "
            "dismiss them first."
        )


def archive_session(directory):
    """Archive a stopped session; the app archives a running one through its supervisor."""
    check_stopped(directory)
    return set_archived(directory, True)


def _discard_worktrees(current, worktree_cleanup):
    """Discard each stopped worktree through the injected retirement cleanup path."""
    for record in worktrees.inventory(current):
        if record["phase"] == "removed":
            continue
        if worktree_cleanup is None:
            raise CaptainError("Worktree cleanup is unavailable.")
        if not worktrees.stopped(current, record):
            continue
        manifest = results.collect(current, record["id"], ["."])
        results.discard(current, manifest["id"], "Session deletion discarded this worktree.")
        worktree_cleanup(current, record["id"])


def session_directory_checked(directory):
    """The directory itself when it is a real session directory under a captain root."""
    sessions = directory.parent
    roots = {state_root().resolve(), temp_root().resolve()}
    if (
        not SESSION_ID.fullmatch(directory.name)
        or directory.is_symlink()
        or sessions.name != "sessions"
        or sessions.is_symlink()
        or sessions.parent.parent.resolve() not in roots
        or not (directory / "session.json").is_file()
    ):
        raise CaptainError(f"[delete-outside-session] {directory} is not a captain session.")
    return directory


def delete_session(directory, *, force=False, discard_worktrees=False, worktree_cleanup=None):
    """Remove one stopped session after applying the caller's explicit discard choices."""
    try:
        directory = session_directory_checked(directory)
    except CaptainError as exc:
        raise SessionDeleteError(str(exc), []) from exc
    with lock(directory / "session.lock"):
        try:
            check_stopped(directory)
        except CaptainError as exc:
            raise SessionDeleteError(str(exc), []) from exc
        current = Session(directory, read_json(directory / "session.json"))
        cleanup_errors = []
        if discard_worktrees:
            try:
                _discard_worktrees(current, worktree_cleanup)
            except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
                cleanup_errors.append(
                    _blocker("worktree", f"A crew worktree could not be discarded safely: {exc}.")
                )
        blockers = session_blockers(directory) + cleanup_errors
        remaining = [
            blocker
            for blocker in blockers
            if not force or blocker["kind"] not in FORCEABLE_BLOCKERS
        ]
        if remaining:
            kinds = {blocker["kind"] for blocker in remaining}
            code = (
                "session-unlanded-work"
                if "worktree" in kinds
                else "session-unreviewed-results"
                if kinds == {"result"}
                else "session-unresolved"
            )
            raise SessionDeleteError(
                f"[{code}] Session {directory.name} cannot be deleted until its blockers are "
                "resolved or explicitly discarded.",
                remaining,
            )
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise SessionDeleteError(
                f"[delete-failed] Session {directory.name} was only partly removed: {exc}", []
            ) from exc
    return {"session_id": directory.name, "deleted": True}
