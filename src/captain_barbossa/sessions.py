"""The session directory lifecycle: open one, create one, name its crew, prune the dead."""

import re
import shutil
import time
import uuid
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

from .eventlog import record_action
from .runtime import HERDR_ERRORS, CaptainError, herdr
from .store import (
    lock,
    private_dir,
    read_json,
    read_storage,
    state_root,
    state_storage,
    storage,
    temp_root,
    write_json,
)


def session_bases(project):
    """Project namespaces that may hold sessions: durable first, then the legacy temp root.

    New sessions are durable; one started before that stays where it was made and is
    opened in place, never moved, so each session has exactly one authoritative directory.
    """
    bases = []
    for root in (state_root(), temp_root()):
        base = read_storage(root, project)
        if base not in bases:
            bases.append(base)
    return bases


def locate(project, session_id):
    """The one existing directory for session_id, or None; never creates anything."""
    found = []
    for base in session_bases(project):
        directory = base / "sessions" / session_id
        if directory.resolve() != base.resolve() / "sessions" / session_id:
            raise CaptainError("Session memory must not be a symlink.")
        path = directory / "session.json"
        if path.is_symlink():
            raise CaptainError("Session metadata must not be a symlink.")
        if path.is_file():
            found.append(directory)
    if len(found) > 1:
        raise CaptainError(
            f"[duplicate-session] Session {session_id} exists in both {found[0]} and "
            f"{found[1]}; remove the stale copy."
        )
    return found[0] if found else None


def is_durable(directory, project):
    """Whether a session lives under the durable state root rather than the OS temp root."""
    return directory.parent.parent == read_storage(state_root(), project)


def session_directories(project):
    """Every session directory of this project, across both roots."""
    for base in session_bases(project):
        sessions = base / "sessions"
        if sessions.is_symlink() or not sessions.is_dir():
            continue
        for directory in sorted(sessions.iterdir()):
            if SESSION_ID.fullmatch(directory.name) and directory.is_dir():
                if not directory.is_symlink():
                    yield directory


def all_sessions(current):
    """Every session of every project in both roots, current project first, newest first.

    Rows are (project, name, id, last activity, directory). The project comes from
    session.json, and a directory without a readable one still lists under "-".
    """
    rows, seen = [], set()
    for root in (state_root(), temp_root()):
        if root.is_symlink() or not root.is_dir():
            continue
        for sessions in sorted(root.glob("*/sessions")):
            if sessions.is_symlink() or not sessions.is_dir():
                continue
            for directory in sorted(sessions.iterdir()):
                if not (SESSION_ID.fullmatch(directory.name) and directory.is_dir()):
                    continue
                if directory.is_symlink() or directory.name in seen:
                    continue
                seen.add(directory.name)
                try:
                    meta = read_json(directory / "session.json")
                    project = str(meta["project"])
                except (CaptainError, OSError, KeyError, TypeError):
                    project = "-"
                rows.append(
                    (
                        project,
                        session_name(directory),
                        directory.name,
                        newest_mtime(directory),
                        directory,
                    )
                )
    rows.sort(key=lambda row: (row[0] != str(current), row[0], -row[3]))
    return rows


def catalog(current):
    """Every project's sessions as JSON for an app's session list, in all_sessions order."""
    projects = {}
    for project, name, session_id, active, directory in all_sessions(current):
        try:
            meta = read_json(directory / "session.json")
        except CaptainError:
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        transport = "supervisor" if meta.get("transport") == "supervisor" else "herdr"
        if transport == "herdr":
            state = "herdr"
        else:
            from .supervision import alive  # local: Herdr commands never load the door

            state = (
                "running" if alive(directory) else "ended" if meta.get("ended_at") else "stopped"
            )
        row = projects.setdefault(
            project, {"project": project, "exists": Path(project).is_dir(), "sessions": []}
        )
        row["sessions"].append(
            {
                "id": session_id,
                "name": name,
                "transport": transport,
                "state": state,
                "last_activity": active,
                "ended_at": meta.get("ended_at"),
                "crew": session_crew(directory),
            }
        )
    return {"projects": list(projects.values())}


def project_sessions(project):
    """Every readable session belonging to the current project, newest first."""
    current = str(project.resolve())
    rows = [row for row in all_sessions(project) if row[0] == current]
    rows.sort(key=lambda row: -row[3])
    return rows


def session_name(directory):
    """The name given by `captain --rename`, or None."""
    try:
        return read_json(directory / "name.json").get("name")
    except (CaptainError, OSError, AttributeError):
        return None


def session_crew(directory):
    """Names on the session's crew roster, oldest first."""
    try:
        roster = read_json(directory / "session.json").get("crew", {})
        return [record.get("name", key) for key, record in roster.items()]
    except (CaptainError, OSError, AttributeError):
        return []


def session_card(row, now=None):
    """(title, "10h ago · Jack, Elizabeth +2") for the resume picker; never the id."""
    seconds = max(0, (now or time.time()) - row[3])
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            ago = f"{int(seconds // size)}{unit} ago"
            break
    else:
        ago = "just now"
    names = session_crew(row[4])
    crew = ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    return row[1] or "Untitled session", " · ".join(filter(None, (ago, crew)))


def rename_session(directory, name, *, if_unnamed=False):
    """if_unnamed never replaces a name; a supervised session logs each change."""
    name = name.strip()
    if not name or len(name) > 60 or SESSION_ID.fullmatch(name):
        raise CaptainError("A session name is 1-60 characters and not a session id.")
    with lock(directory / "session.lock"):
        previous = session_name(directory)
        if if_unnamed and previous:
            return
        write_json(directory / "name.json", {"name": name})
        if name != previous:
            record_action(
                directory,
                "session-renamed",
                None,
                None,
                supervised=_is_supervised(directory),
                session_id=directory.name,
                name=name,
                previous=previous,
            )


def _is_supervised(directory):
    try:
        return read_json(directory / "session.json").get("transport") == "supervisor"
    except (CaptainError, OSError, AttributeError):
        return False


def find_session(current, target):
    """The one session whose id or name is target; an ambiguous name is an error."""
    rows = all_sessions(current)
    found = [row for row in rows if target == row[2]] or [row for row in rows if target == row[1]]
    if not found:
        raise CaptainError(f"No session named or numbered {target}.")
    if len(found) > 1:
        ids = ", ".join(f"{row[2]} ({row[0]})" for row in found)
        raise CaptainError(
            f"[ambiguous-session] {target} names several sessions: {ids}. Use an id."
        )
    return found[0]


def read_session(project, session_id, pane=None, *, max_bytes=None):
    """Open only the selected session's metadata; never initialize or repair state."""
    if not session_id:
        raise CaptainError("Start captain first, or pass --session <id>.")
    if not SESSION_ID.fullmatch(session_id):
        raise CaptainError("Invalid captain session ID.")
    directory = locate(project, session_id)
    if directory is None:
        raise CaptainError("This session does not exist for the current project.")
    meta = read_json(directory / "session.json", max_bytes=max_bytes)
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


def workspace_gone(workspace):
    """Only a successful, well-formed Herdr listing proves a workspace is gone."""
    try:
        rows = herdr("workspace", "list", timeout=10).get("workspaces")
    except HERDR_ERRORS:
        return False
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not row.get("workspace_id") for row in rows
    ):
        return False
    return all(row["workspace_id"] != workspace for row in rows)


def session(project, pane, session_id=None, create=False, *, allow_dead_workspace=False):
    project = project.resolve()
    if session_id is None:
        if not create:
            raise CaptainError("Start captain first, or pass --session <id>.")
        session_id = uuid.uuid4().hex
    if not SESSION_ID.fullmatch(session_id):
        raise CaptainError("Invalid captain session ID.")
    directory = locate(project, session_id)
    if directory is None:
        if not create:
            raise CaptainError("This session does not exist for the current project.")
        directory = private_dir(state_storage(project) / "sessions") / session_id
    private_dir(directory)
    with lock(directory / "session.lock"):
        meta_path = directory / "session.json"
        if meta_path.exists():
            meta = read_json(meta_path)
            if meta["project"] != str(project) or (
                pane is not None
                and meta["workspace"] != pane["workspace_id"]
                and not (allow_dead_workspace and workspace_gone(meta["workspace"]))
            ):
                raise CaptainError("This session belongs to another project or Herdr workspace.")
        else:
            meta = {
                "id": session_id,
                "project": str(project),
                "workspace": pane["workspace_id"] if pane is not None else None,
                "crew": {},
            }
            write_json(meta_path, meta)
        if pane is not None and meta["workspace"] == pane["workspace_id"]:
            backfill_terminal_id(directory, pane)
    try:
        migrate_project_graph(project)
    except (CaptainError, OSError):
        pass  # migration is housekeeping; never block a command on it
    return Session(directory, meta)


def newest_mtime(directory):
    """Last activity under a session. Naming is not activity: a rename writes name.json,
    may create session.lock and bumps the directory's own mtime, so none of them count."""
    newest = 0
    for path in directory.rglob("*"):
        if path in (directory / "session.lock", directory / "name.json"):
            continue
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest
