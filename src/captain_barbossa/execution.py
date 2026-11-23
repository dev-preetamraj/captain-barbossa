"""Resolve registered crew execution roots and the active project without moving coordination state."""

import os
from pathlib import Path

from . import worktrees
from .runtime import CaptainError
from .sessions import read_session
from .store import project_root


def root(session, record, *, deadline=None):
    project = Path(session.meta["project"]).resolve()
    identifier = record.get("worktree_id")
    if not identifier:
        return project
    registered = worktrees.read(session, identifier, deadline=deadline)
    for key in ("incarnation_id", "assignment_id"):
        if registered[key] != record.get(key):
            raise CaptainError("[worktree-identity] Execution root belongs to another assignment.")
    crew_id = record.get("crew_id", record.get("id"))
    if registered["crew_id"] != crew_id or Path(registered["project"]).resolve() != project:
        raise CaptainError("[worktree-identity] Execution root belongs to another crew or project.")
    return Path(registered["execution_root"])


def active_project():
    """The project root, refusing a cwd outside it unless that cwd is in this crew's
    launch-bound, registered worktree."""
    project = project_root()
    if os.environ.get("CAPTAIN_PROJECT"):
        cwd = Path.cwd().resolve()
        if not cwd.is_relative_to(project) and not cwd.is_relative_to(current(project)):
            raise CaptainError("This directory is outside the active captain project.")
    return project


def current(project, session_id=None, *, deadline=None):
    """Resolve only the launch-bound incarnation; never trust an environment path."""
    identifier = os.environ.get("CAPTAIN_WORKTREE_ID")
    if not identifier:
        return Path(project).resolve()
    session = read_session(project, session_id or os.environ.get("CAPTAIN_SESSION"))
    crew_id = os.environ.get("CAPTAIN_CREW")
    record = session.meta.get("crew", {}).get(crew_id, {})
    if (
        record.get("worktree_id") != identifier
        or record.get("incarnation_id") != os.environ.get("CAPTAIN_INCARNATION")
        or record.get("assignment_id") != os.environ.get("CAPTAIN_ASSIGNMENT")
    ):
        raise CaptainError("[worktree-identity] Stale or unregistered crew execution root.")
    return root(session, record, deadline=deadline)
