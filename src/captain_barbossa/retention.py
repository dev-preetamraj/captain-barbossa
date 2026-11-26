"""Session retention: prune old session directories with no live agent or unresolved work."""

import shutil
import time

from . import lifetime, native_history, results, worktrees
from .runtime import HERDR_ERRORS, CaptainError, herdr
from .sessions import Session, crew_prefix, newest_mtime, session_directories
from .store import lock, read_json

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
            if agents is not None:
                terminal_ids, names = agents
                prefix = crew_prefix(directory.name)
                terminal_id = session_terminal_id(directory)
                if (terminal_id and terminal_id in terminal_ids) or any(
                    name.startswith(prefix) for name in names
                ):
                    continue
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append(directory)
    return removed
