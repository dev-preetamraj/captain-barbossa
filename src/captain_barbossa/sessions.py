"""The session directory lifecycle: open one, create one, name its crew, prune the dead."""

import re
import shutil
import time
import uuid
from collections import namedtuple
from contextlib import contextmanager

from .runtime import HERDR_ERRORS, CaptainError, herdr
from .store import (
    lock,
    private_dir,
    read_json,
    read_storage,
    state_storage,
    storage,
    temp_root,
    write_json,
)


def read_session(project, session_id, pane=None, *, max_bytes=None):
    """Open only the selected session's metadata; never initialize or repair state."""
    if not session_id:
        raise CaptainError("Start captain first, or pass --session <id>.")
    if not SESSION_ID.fullmatch(session_id):
        raise CaptainError("Invalid captain session ID.")
    base = read_storage(temp_root(), project)
    directory = base / "sessions" / session_id
    if directory.resolve() != base.resolve() / "sessions" / session_id:
        raise CaptainError("Session memory must not be a symlink.")
    path = directory / "session.json"
    if path.is_symlink():
        raise CaptainError("Session metadata must not be a symlink.")
    if not path.is_file():
        raise CaptainError("This session does not exist for the current project.")
    meta = read_json(path, max_bytes=max_bytes)
    if not isinstance(meta, dict) or meta.get("project") != str(project.resolve()):
        raise CaptainError("This session belongs to another project.")
    if pane is not None and meta.get("workspace") != pane["workspace_id"]:
        raise CaptainError("This session belongs to another Herdr workspace.")
    return Session(directory, meta)


def migrate_project_graph(project):
    """Copy a pre-split project graph from the temp root into the state root, once."""
    source = storage(project) / "graph.json"
    if not source.is_file():
        return
    destination = state_storage(project) / "graph.json"
    if source == destination or destination.exists():
        return
    with lock(destination.parent / "graph.lock"):
        if not destination.exists():
            shutil.copyfile(source, destination)
            destination.chmod(0o600)


def backfill_terminal_id(directory, pane):
    """Record the Herdr terminal ID on a captain.json written before it was tracked.

    Without it prune_sessions cannot tell a live pre-fix captain from a dead one.
    Crew run the same commands, so only the captain's own pane may claim it.
    """
    path = directory / "captain.json"
    terminal_id = pane.get("terminal_id")
    if not terminal_id or not path.is_file():
        return
    try:
        data = read_json(path)
    except CaptainError:
        return
    if not isinstance(data, dict) or data.get("terminal_id"):
        return
    if data.get("pane") == pane["pane_id"]:
        write_json(path, {**data, "terminal_id": terminal_id})


SESSION_ID = re.compile(r"[a-f0-9]{32}")


class Session(namedtuple("Session", "directory meta")):
    """A session directory and its metadata, plus the paths that live under it."""

    @property
    def graph(self):
        return self.directory / "graph.json"

    @property
    def meta_path(self):
        return self.directory / "session.json"

    def events(self, crew_id):
        return self.directory / "events" / f"{crew_id}.jsonl"


def crew_prefix(session_id):
    return f"c-{session_id[:8]}-"


def agent_name(session_id, name):
    """The Herdr agent name for a crew; prune_sessions reads live crew back off this prefix."""
    return f"{crew_prefix(session_id)}{name}"


@contextmanager
def crew_meta(directory):
    """Hold the crew lock over a read-modify-write of session.json."""
    with lock(directory / "crew.lock"):
        meta = read_json(directory / "session.json")
        yield meta
        write_json(directory / "session.json", meta)


def session(project, pane, session_id=None, create=False):
    project = project.resolve()
    if session_id is None:
        if not create:
            raise CaptainError("Start captain first, or pass --session <id>.")
        session_id = uuid.uuid4().hex
    if not SESSION_ID.fullmatch(session_id):
        raise CaptainError("Invalid captain session ID.")
    base = storage(project)
    directory = private_dir(base / "sessions") / session_id
    if not create and not (directory / "session.json").is_file():
        raise CaptainError("This session does not exist for the current project.")
    private_dir(directory)
    with lock(directory / "session.lock"):
        meta_path = directory / "session.json"
        if meta_path.exists():
            meta = read_json(meta_path)
            if meta["project"] != str(project) or meta["workspace"] != pane["workspace_id"]:
                raise CaptainError("This session belongs to another project or Herdr workspace.")
        else:
            meta = {
                "id": session_id,
                "project": str(project),
                "workspace": pane["workspace_id"],
                "crew": {},
            }
            write_json(meta_path, meta)
        backfill_terminal_id(directory, pane)
    try:
        migrate_project_graph(project)
    except (CaptainError, OSError):
        pass  # migration is housekeeping; never block a command on it
    return Session(directory, meta)


PRUNE_DAYS = 7


def newest_mtime(directory):
    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest


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


def prune_sessions(project, days=PRUNE_DAYS, current=None):
    """Remove session directories older than `days` that hold no live Herdr agent.

    Herdr being unreachable makes liveness unknowable, so the cutoff doubles rather
    than guessing. Returns the directories removed.
    """
    sessions = storage(project) / "sessions"
    if not sessions.is_dir():
        return []
    agents = live_agents()
    cutoff = time.time() - days * 86400 * (2 if agents is None else 1)
    removed = []
    for directory in sorted(sessions.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not SESSION_ID.fullmatch(directory.name) or directory.name == current:
            continue
        if newest_mtime(directory) >= cutoff:
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
