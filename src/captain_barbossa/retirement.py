"""Retire crew incarnations: stop their managed writers, then clean or keep their worktrees and results."""

from pathlib import Path

from . import lifetime, native_history, protocol, results, store, worktrees
from .crew import Crew
from .runtime import CaptainError
from .store import read_json


def artifact_command(args, session):
    """Captain-only artifact operations; no CLI claim substitutes for stopped writers."""
    if args.command == "worktree":
        if args.worktree_command == "list":
            return worktrees.inventory(session)
        record = worktrees.read(session, args.worktree_id, validate=False)
        ref = record["incarnation_id"]
        verdict = stop(session, ref)
        crew = Crew(
            record["crew_id"],
            {"id": record["crew_id"], "incarnation_id": record["incarnation_id"]},
            session,
        )
        history = native_history.cleanup(crew)
        if not verdict.ok:
            raise CaptainError(
                f"[cleanup-pending] {verdict.reason}; native history: {history['state']}"
            )
        _no_cwd_holders(session, args.worktree_id)
        return dispose(session, args.worktree_id)
    if args.result_command == "collect":
        record = worktrees.read(session, args.worktree_id)
        assignment = read_json(session.directory / "protocol.json")["assignments"][
            record["assignment_id"]
        ]
        if assignment["incarnation_id"] != record["incarnation_id"]:
            raise CaptainError("[worktree-identity] Result assignment incarnation mismatch.")
        return results.collect(
            session,
            record["id"],
            assignment["paths"],
            assignment.get("report") or "",
        )
    if args.result_command == "show":
        return results.preview(session, args.result_id)
    if args.result_command == "apply":
        return results.apply(session, args.result_id)
    return results.discard(session, args.result_id, args.reason)


def retire(crew):
    """Stop the registered incarnation; keep unresolved artifacts independently."""
    # background imports execution for launch roots.
    from . import background

    if crew.is_background and background.alive(crew):
        background.cancel(crew, "Crew dismissed.")
    ref = crew.record["lifetime_id"]
    verdict = stop(crew.session, ref)
    state = {"lifetime": {"ok": verdict.ok, "reason": verdict.reason}}
    state["history"] = native_history.cleanup(crew)
    if crew.record.get("worktree_id"):
        assignment = protocol.active_assignment(crew)
        if assignment is None:
            state["worktree"] = "retained: no assignment"
        else:
            try:
                manifest = _final_result(crew.session, crew.record["worktree_id"])
                if manifest is None:
                    manifest = results.collect(
                        crew.session,
                        crew.record["worktree_id"],
                        assignment["paths"],
                        assignment.get("report") or "",
                    )
                state["result_id"] = manifest["id"]
                if verdict.ok:
                    _no_cwd_holders(crew.session, crew.record["worktree_id"])
                    state["worktree"] = dispose(crew.session, crew.record["worktree_id"])[
                        "cleanup_state"
                    ]
                else:
                    state["worktree"] = f"retained: {verdict.reason}"
            except CaptainError as exc:
                state["worktree"] = f"retained: {exc}"
    return state


def _no_cwd_holders(session, worktree_id):
    """A process still working inside the worktree may be an escaped writer: retain."""
    root = Path(worktrees.read(session, worktree_id, validate=False)["execution_root"])
    if not root.exists():
        return
    holders = lifetime.cwd_holders(root)
    if holders:
        raise CaptainError(
            f"[cleanup-pending] processes {', '.join(map(str, holders))} run inside the worktree."
        )


def _final_result(session, worktree_id):
    """Keep an already-final result, so its apply/discard settlement stays authoritative."""
    record = worktrees.read(session, worktree_id, validate=False)
    if not record.get("result_id"):
        return None
    manifest = results.read(session, record["result_id"])
    # Disposal re-verifies that the worktree still matches this final snapshot.
    return None if manifest["provisional"] else manifest


def stop(session, ref):
    """Revoke a launch and consult authoritative exit evidence after any terminal close."""
    record = lifetime.read(session, ref)
    if record["pane"]:
        lifetime.close(session, ref)
    else:
        lifetime.revoke(session, ref)
    return lifetime.check(
        session,
        ref,
        crew_id=record["crew_id"],
        incarnation_id=record["incarnation_id"],
        root=record["root"],
        worktree=record["worktree_id"],
        scope="worktree" if record["worktree_id"] else "group",
    )


def dispose(session, worktree_id):
    with store.lock(session.directory / "worktrees.lock"):
        record = worktrees.read(session, worktree_id, validate=False)
        if record["phase"] == "removed":
            return record
        if not worktrees.stopped(session, record):
            raise CaptainError("Cleanup requires verified managed-writer termination.")
        root = Path(record["execution_root"])
        admin = Path(record["worktree_git_dir"] or record["common_git_dir"])
        if record["phase"] == "removing" and not root.exists() and not admin.exists():
            record.update(phase="removed", cleanup_state="removed")
            worktrees.durable(worktrees.record_path(session, worktree_id), record)
            return record
        worktrees.validate_registration(record)
        results.cleanup_ready(session, record)
        record.update(phase="removing", cleanup_state="removing")
        worktrees.durable(worktrees.record_path(session, worktree_id), record)
        worktrees.git(record["project"], "worktree", "remove", "--force", root)
        if root.exists() or admin.exists():
            raise CaptainError("Worktree removal incomplete; journal retained.")
        record.update(phase="removed", cleanup_state="removed")
        worktrees.durable(worktrees.record_path(session, worktree_id), record)
        return record
