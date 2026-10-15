"""Durable identities and conservative lifecycle for isolated crew checkouts."""

import os
import re
import subprocess
import time
from pathlib import Path

from . import inspection, lifetime, sessions, store
from .runtime import CaptainError


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise CaptainError("Invalid worktree identity.")
    return value


def plain_path(path):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise CaptainError(f"Symlinked worktree path: {path}")
    return path


def durable(path, value):
    """Flush the journal's directory entry as well as its contents."""
    plain_path(path)
    store.write_json(path, value)
    # Flush newly created journal ancestors too, not just the file's rename.
    for parent in path.parents:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def git(root, *args):
    """Run local Git with no inherited Git environment, hooks or network."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/dev/null",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    command = [str(inspection.GIT), "--no-pager"]
    for setting in (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "core.attributesFile=/dev/null",
        "protocol.allow=never",
        "maintenance.auto=false",
        "gc.auto=0",
    ):
        command.extend(("-c", setting))
    try:
        result = subprocess.run(
            [*command, *map(str, args)],
            cwd=root,
            env=env,
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptainError(f"Worktree Git failed: {exc}") from exc
    if result.returncode:
        raise CaptainError(f"Worktree Git failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _project(session):
    project = plain_path(session.meta["project"])
    if not sessions.is_durable(session.directory, project):
        raise CaptainError("Worktrees require a durable session.")
    plain_path(session.directory)
    if session.directory.name != validate_id(session.meta["id"]):
        raise CaptainError("Session identity mismatch.")
    return project


def _budget(deadline):
    deadline = time.monotonic() + inspection.TIMEOUT if deadline is None else deadline
    inspection.check_deadline(deadline)
    return deadline


def _pointer(root, path, deadline):
    inspection.check_deadline(deadline)
    try:
        data, cut = inspection.read_bytes(root, path, limit=4096)
    except OSError as exc:
        raise CaptainError("Unreadable or unsafe worktree pointer.") from exc
    inspection.check_deadline(deadline)
    if cut:
        raise CaptainError("Worktree pointer exceeds 4096 bytes.")
    return inspection.decode_text(data).strip()


def validate_repository(project, *, deadline=None):
    deadline = _budget(deadline)
    common = plain_path(project / ".git")
    if not common.is_dir():
        raise CaptainError("Worktree creation requires an ordinary Git checkout.")
    inspection.validate_git_config(project)
    for path in inspection.walk_files(project, Path(".git"), deadline, metadata=True):
        if (
            (path.name in ("commondir", "gitdir") and path.parts[:2] != (".git", "worktrees"))
            or path.name in ("alternates", "http-alternates", "config.worktree")
            or path.suffix == ".promisor"
        ):
            raise CaptainError("Unsupported Git object store or worktree configuration.")
    inspection.check_deadline(deadline)
    return common


def record_path(session, worktree_id):
    return plain_path(session.directory / "worktrees" / f"{validate_id(worktree_id)}.json")


def reserve(session, crew_id, incarnation_id, assignment_id):
    project = _project(session)
    common = validate_repository(project)
    worktree_id = validate_id(incarnation_id)
    validate_id(assignment_id)
    base = git(project, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    root = session.directory.parent.parent / "worktrees" / session.meta["id"] / worktree_id
    plain_path(root)
    record = {
        "schema": 1,
        "id": worktree_id,
        "project": str(project),
        "session_id": session.meta["id"],
        "crew_id": crew_id,
        "incarnation_id": incarnation_id,
        "assignment_id": assignment_id,
        "execution_root": str(root),
        "common_git_dir": str(common),
        "worktree_git_dir": None,
        "base_oid": base,
        "destination_root": str(project),
        "phase": "reserved",
        "result_id": None,
        "cleanup_state": "retained",
        "failure_reason": None,
    }
    store.private_dir(record_path(session, worktree_id).parent)
    with store.lock(session.directory / "worktrees.lock"):
        if record_path(session, worktree_id).exists() or root.exists():
            raise CaptainError("This incarnation already has a worktree reservation.")
        durable(record_path(session, worktree_id), record)
    return record


def read(session, worktree_id, validate=True, *, deadline=None):
    deadline = _budget(deadline)
    project = _project(session)
    record = store.read_json(record_path(session, worktree_id), max_bytes=inspection.MAX_BYTES)
    expected = session.directory.parent.parent / "worktrees" / session.meta["id"] / worktree_id
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or record.get("id") != worktree_id
        or record.get("incarnation_id") != worktree_id
        or record.get("session_id") != session.meta["id"]
        or record.get("project") != str(project)
        or record.get("destination_root") != str(project)
        or record.get("execution_root") != str(expected)
        or record.get("common_git_dir") != str(project / ".git")
        or not isinstance(record.get("base_oid"), str)
        or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", record["base_oid"])
    ):
        raise CaptainError("Worktree identity mismatch.")
    validate_id(record.get("assignment_id"))
    plain_path(expected)
    if validate:
        validate_registration(record, deadline=deadline)
    _budget(deadline)
    return record


def validate_registration(record, *, deadline=None):
    deadline = _budget(deadline)
    root = plain_path(record["execution_root"])
    common = validate_repository(Path(record["project"]), deadline=deadline)
    admin = plain_path(record["worktree_git_dir"] or common / "missing")
    if admin.parent != common / "worktrees" or not admin.is_dir():
        raise CaptainError("Worktree administration identity mismatch.")
    pointer = _pointer(root, Path(".git"), deadline)
    reciprocal = _pointer(common, admin.relative_to(common) / "gitdir", deadline)
    common_pointer = _pointer(common, admin.relative_to(common) / "commondir", deadline)
    if (
        pointer != f"gitdir: {admin}"
        or reciprocal != str(root / ".git")
        or (admin / common_pointer).resolve() != common
    ):
        raise CaptainError("Worktree registration is not reciprocal.")
    _budget(deadline)


def create(session, worktree_id):
    with store.lock(session.directory / "worktrees.lock"):
        record = read(session, worktree_id, validate=False)
        if record["phase"] != "reserved":
            raise CaptainError("Worktree creation is not repeatable; retain interrupted creation.")
        root = Path(record["execution_root"])
        store.private_dir(root.parent)
        record["phase"] = "creating"
        durable(record_path(session, worktree_id), record)
        try:
            validate_repository(Path(record["project"]))
            git(record["project"], "worktree", "add", "--detach", root, record["base_oid"])
            pointer = _pointer(root, Path(".git"), _budget(None))
            if not pointer.startswith("gitdir: "):
                raise CaptainError("Invalid worktree pointer.")
            record["worktree_git_dir"] = pointer[8:]
            validate_registration(record)
            record["phase"] = "ready"
        except (CaptainError, OSError) as exc:
            record["failure_reason"] = str(exc)
            durable(record_path(session, worktree_id), record)
            raise
        durable(record_path(session, worktree_id), record)
        return record


def inventory(session):
    directory = plain_path(session.directory / "worktrees")
    return [read(session, path.stem, validate=False) for path in sorted(directory.glob("*.json"))]


def stopped(session, record):
    """Reread the authoritative lifetime binding; caller assertions grant nothing."""
    return lifetime.check(
        session,
        record["incarnation_id"],
        crew_id=record["crew_id"],
        incarnation_id=record["incarnation_id"],
        root=record["execution_root"],
        worktree=record["id"],
    ).ok


def dispose(session, worktree_id):
    # results imports worktrees for identity and Git operations.
    from . import results

    with store.lock(session.directory / "worktrees.lock"):
        record = read(session, worktree_id, validate=False)
        if record["phase"] == "removed":
            return record
        if not stopped(session, record):
            raise CaptainError("Cleanup requires verified managed-writer termination.")
        root = Path(record["execution_root"])
        admin = Path(record["worktree_git_dir"] or record["common_git_dir"])
        if record["phase"] == "removing" and not root.exists() and not admin.exists():
            record.update(phase="removed", cleanup_state="removed")
            durable(record_path(session, worktree_id), record)
            return record
        validate_registration(record)
        results.cleanup_ready(session, record)
        record.update(phase="removing", cleanup_state="removing")
        durable(record_path(session, worktree_id), record)
        git(record["project"], "worktree", "remove", "--force", root)
        if root.exists() or admin.exists():
            raise CaptainError("Worktree removal incomplete; journal retained.")
        record.update(phase="removed", cleanup_state="removed")
        durable(record_path(session, worktree_id), record)
        return record
