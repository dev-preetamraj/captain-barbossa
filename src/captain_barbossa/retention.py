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


def unresolved(directory):
    """Whether a session holds work or an outcome nobody has received yet, kept at any age.

    A dismissed crew can receive nothing more, so only live roster entries count. State
    that cannot be read cannot prove there is nothing left, so it counts too, and so does
    a supervised session whose supervisor still holds its lock.
    """
    try:
        path = directory / "session.json"
        meta = read_json(path) if path.exists() else {}
        if meta.get("transport") == "supervisor":
            from . import supervision  # local: Herdr sessions never load the supervisor door

            if supervision.alive(directory):
                return True
        roster = meta.get("crew", {})
        current = Session(directory, meta)
        lifetimes = directory / "lifetimes"
        if (
            (lifetimes.exists() or lifetimes.is_symlink()) and lifetime.pending(directory)
        ) or native_history.pending(directory):
            return True
        records = worktrees.inventory(current)
        if any(record["phase"] != "removed" for record in records):
            return True
        manifests = results.inventory(current)
        if any(
            item["worktree_id"] not in {record["id"] for record in records} for item in manifests
        ):
            return True
        path = directory / "protocol.json"
        state = read_json(path) if path.exists() else {}
        for crew_id, assignment_id in state.get("active", {}).items():
            record = roster.get(crew_id)
            assignment = state["assignments"].get(assignment_id)
            if not assignment:
                continue
            if record is None:
                # Reserved before its roster entry: a recruit that died part-way.
                if assignment["state"] != "done":
                    return True
                continue
            dismissed = record.get("status") == "dismissed"
            if dismissed or assignment["incarnation_id"] != record.get("incarnation_id"):
                continue
            if (
                assignment["state"] != "done"
                or assignment["pending"]
                or assignment["ack_seq"] < len(assignment["notices"])
            ):
                return True
    except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError):
        return True
    return False


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


def unlanded_worktrees(current):
    """One line per crew worktree not yet removed, since its owned work may not have landed."""
    return [
        f"worktree {record['id']} (crew {record['crew_id']}, {record['phase']}) "
        f"at {record['execution_root']}"
        for record in worktrees.inventory(current)
        if record["phase"] != "removed"
    ]


def unreviewed_results(current):
    """One line per result neither applied, discarded nor marked reviewed."""
    path = current.directory / "protocol.json"
    state = read_json(path) if path.exists() else {}
    reviewed = {
        result_id
        for assignment in state.get("assignments", {}).values()
        for result_id in assignment.get("reviewed_results") or {}
    }
    lines = []
    for manifest in results.inventory(current):
        folder = current.directory / "results" / manifest["id"]
        receipt = folder / "transfer.json"
        applied = receipt.is_file() and read_json(receipt).get("state") == "applied"
        if manifest["id"] in reviewed or applied or (folder / "discard.json").exists():
            continue
        lines.append(
            f"result {manifest['id']} (crew {manifest['crew_id']}, "
            f"assignment {manifest['assignment_id']})"
        )
    return lines


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


def delete_session(directory):
    """Remove one stopped session's directory, refusing while work or agents remain in it.

    Crew worktrees live outside the directory, so one not yet removed or a result not yet
    landed, discarded or reviewed refuses, listing each; nothing outside the directory is touched.
    """
    directory = session_directory_checked(directory)
    with lock(directory / "session.lock"):
        check_stopped(directory)
        current = Session(directory, read_json(directory / "session.json"))
        try:
            held = unlanded_worktrees(current)
            unreviewed = unreviewed_results(current)
        except (CaptainError, OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
            raise CaptainError(
                f"[session-unresolved] Session {directory.name} has unreadable worktree or "
                f"result state ({exc}); keeping it."
            ) from exc
        if held:
            raise CaptainError(
                f"[session-unlanded-work] Session {directory.name} has crew worktrees not yet "
                f"removed: {'; '.join(held)}. Land or discard their results, then remove them."
            )
        if unreviewed:
            raise CaptainError(
                f"[session-unreviewed-results] Session {directory.name} has unreviewed "
                f"results: {'; '.join(unreviewed)}. Apply, discard or review them first."
            )
        if unresolved(directory):
            raise CaptainError(
                f"[session-unresolved] Session {directory.name} holds work or an outcome nobody "
                "has received yet."
            )
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise CaptainError(
                f"[delete-failed] Session {directory.name} was only partly removed: {exc}"
            ) from exc
    return {"session_id": directory.name, "deleted": True}
