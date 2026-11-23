"""Background crew run files and whether an owner still holds them, read without the protocol."""

import fcntl
from contextlib import contextmanager

from .runtime import CaptainError
from .store import lock, private_dir, read_json

FAILED = ("failed", "cancelled", "lost")


UNRESERVED = "[background-start] recruitment stopped before it was reserved"


def stem(crew):
    return crew.session.directory / "background" / f"{crew.crew_id}-{crew.record['incarnation_id']}"


def paths(base):
    return {
        kind: base.with_name(f"{base.name}{suffix}")
        for kind, suffix in (
            ("record", ".json"),
            ("lock", ".lock"),
            ("cancel", ".cancel"),
            ("stdout", ".out"),
            ("stderr", ".err"),
            ("report", ".report.md"),
            ("log", ".owner.log"),
            ("launch", ".launch"),
            ("intent", ".intent"),
            ("claimed", ".claimed"),
            ("abandoned", ".abandoned"),
        )
    }


@contextmanager
def held(path):
    """Yield True when this process took the lock, False when someone else holds it."""
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


@contextmanager
def launching(crew):
    """The recruiter's lock until claim or withdrawal, so `alive` can tell a recruiter still
    deciding from one that died before any owner took over."""
    base = stem(crew)
    private_dir(base.parent)
    with lock(paths(base)["launch"]):
        yield


def alive(crew):
    """Whether a recruiter or an owner still holds this crew's run."""
    files = paths(stem(crew))
    try:
        record = read_json(files["record"])
    except CaptainError:
        # Preparation publishes the assignment before writing the run record.
        if files["launch"].exists():
            with held(files["launch"]) as free:
                return not free
        return False
    if record["state"] not in ("launching", "running"):
        return False
    for name in ("launch", "lock") if record["state"] == "launching" else ("lock",):
        with held(files[name]) as free:
            if not free:
                return True
    # A launch nobody holds was left by a dead recruiter; a late owner's claim finds its
    # assignment closed and withdraws.
    return False


def describe(crew, assignment):
    """(state, source, detail) for status, from the assignment first, then the owner."""
    if not assignment:
        return "failed", "process", UNRESERVED
    if assignment.get("outcome") in FAILED:
        return "failed", "process", assignment["report"]
    if assignment["state"] == "done":
        return "done", "mail", assignment.get("report") or "assignment completed"
    if alive(crew):
        return "working", "process", "background owner running"
    # A read path records nothing; the next wait closes it as lost and delivers that.
    return "failed", "process", "[background-lost] owner gone without an outcome"
