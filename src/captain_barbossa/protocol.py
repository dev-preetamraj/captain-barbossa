"""Explicit assignment state and acknowledged delivery, local to one session."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .memory import lock, read_json, write_json
from .models import HOOKLESS
from .pane import DRAFT_GATE
from .runtime import HERDR_ERRORS, CaptainError, check_text

ACTIONS = ("read", "search", "edit", "test", "build", "format", "commit", "version", "delete")
# Native "working" is a wake, not news: it fires at recruit and at every crew turn end.
NOTIFIED = {"done": "idle", "idle": "idle", "blocked": "awaiting_approval"}
APPROVAL_SUMMARY = "Inspect native approval prompt; approval is not granted by this event."
# Claude reports approvals through its PermissionRequest hook; these have no such event.
MODAL_PROVIDERS = ("codex", *HOOKLESS)
MODAL_INTERVAL = 30
# No daemon drains held mail; the wait loop already polls, so it retries the doorbell instead.
DRAIN_INTERVAL = 30
# A ring that landed needs no retry for the crew's sake; this only exists so a pane that died
# after the nudge eventually bounces its mail instead of sitting on it forever.
DRAIN_LANDED_INTERVAL = 600
# A draft gate can hold mail all session, so one hold this long is escalated to the captain.
HELD_NOTICE_INTERVAL = 300


@contextmanager
def checkpoint(directory):
    # ponytail: one session lock; split by assignment only if contention matters.
    with lock(directory / "protocol.lock"):
        path = directory / "protocol.json"
        state = read_json(path) if path.exists() else {"active": {}, "assignments": {}}
        yield state
        write_json(path, state)


def owned_paths(project, paths):
    root = Path(project).resolve()
    result = []
    for value in paths:
        path = (root / value).resolve()
        if not path.is_relative_to(root) or ".git" in path.relative_to(root).parts:
            raise CaptainError(f"Owned path must stay inside the project, outside .git: {value}")
        result.append(path.relative_to(root).as_posix())
    return sorted(set(result))


def overlaps(left, right):
    a, b = Path(left), Path(right)
    return a.is_relative_to(b) or b.is_relative_to(a)


def active(state, crew, assignment_id=None, incarnation=None):
    key = state["active"].get(crew.crew_id)
    assignment = state["assignments"].get(key)
    if not assignment or not crew.record.get("incarnation_id"):
        raise CaptainError("Legacy crew has no protocol assignment; recruit new crew explicitly.")
    if assignment["incarnation_id"] != crew.record["incarnation_id"]:
        raise CaptainError("Stale crew incarnation.")
    if assignment_id is not None and assignment_id != key:
        raise CaptainError("Stale assignment ID.")
    if incarnation is not None and incarnation != assignment["incarnation_id"]:
        raise CaptainError("Stale crew incarnation.")
    return assignment


def begin(crew, project, task, paths=(), actions=(), handoff=None):
    check_text(task, "task")
    paths = owned_paths(project, paths)
    actions = sorted(set(actions) | {"read", "search"})
    if set(actions) - set(ACTIONS):
        raise CaptainError("Unknown assignment action.")
    if not crew.record.get("incarnation_id"):
        raise CaptainError("Legacy crew cannot be silently adopted; recruit new crew.")
    with checkpoint(crew.session.directory) as state:
        previous = state["active"].get(crew.crew_id)
        if handoff and not previous:
            raise CaptainError("No previous assignment matches this handoff.")
        if previous:
            old = state["assignments"][previous]
            if old["state"] != "done" or handoff != previous:
                raise CaptainError(
                    "Reassignment requires done with report and --handoff ASSIGNMENT_ID."
                )
            if old["pending"] or old["ack_seq"] != len(old["notices"]):
                raise CaptainError(
                    "Acknowledge the previous assignment notifications before handoff."
                )
        for other in state["assignments"].values():
            if other["state"] != "done" and any(
                overlaps(a, b) for a in paths for b in other["paths"]
            ):
                raise CaptainError(f"Owned paths overlap active assignment {other['id']}.")
        assignment = {
            "id": uuid4().hex,
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
    assignment["notices"].append({"status": status, "summary": summary[:1600]})


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
        raise CaptainError(f"Action {action!r} was not granted.")
    if action in ("edit", "format", "commit", "version", "delete"):
        if not paths:
            raise CaptainError("This action requires explicit owned paths.")
        for path in owned_paths(project, paths):
            if not any(Path(path).is_relative_to(Path(owner)) for owner in assignment["paths"]):
                raise CaptainError(f"Path is not owned by this assignment: {path}")


def change(crew, args, project):
    assignment_id, incarnation = validate_actor(crew, args)
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id, incarnation)
        if args.command == "check":
            check_action(assignment, project, args.action, args.path)
            return {"assignment_id": assignment_id, "allowed": True}
        if assignment["state"] == "done":
            raise CaptainError("Assignment is already done.")
        if args.command == "ask":
            check_text(args.question, "question")
            if assignment["question"]:
                raise CaptainError("Only one question may be pending.")
            question = {"id": uuid4().hex, "text": args.question}
            assignment["question"] = question
            assignment["state"] = "asked"
            notice(assignment, "asked", f"{question['id']}: {args.question}")
            return {"assignment_id": assignment_id, "question_id": question["id"]}
        check_text(args.report, "report")
        if assignment["question"]:
            raise CaptainError("Answer the pending question before done.")
        if unread(crew):
            raise CaptainError("Unread mail is waiting; run inbox before done.")
        assignment["report"] = args.report
        assignment["state"] = "done"
        notice(assignment, "done", args.report)
        return {"assignment_id": assignment_id, "status": "done"}


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
    write_json(directory / f"{message['id']}.json", message)
    return message["id"]


def queued(session_directory, crew_id):
    """Queued mail, oldest first; the Stop hook reads it without a crew object."""
    directory = mail_dir(session_directory, crew_id)
    if not directory.exists():
        return []
    messages = (read_json(path) for path in directory.glob("*.json"))
    return sorted((m for m in messages if m["state"] == "queued"), key=lambda m: m["queued_at"])


def unread(crew):
    return queued(crew.session.directory, crew.crew_id)


def mark_read(crew, ids):
    """The crew's own read writes the receipt; a bounced message is never resurrected."""
    directory = mail_dir(crew.session.directory, crew.crew_id)
    receipts = {}
    with lock(crew.session.directory / "protocol.lock"):
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
        with checkpoint(crew.session.directory) as state:
            for assignment_id, read_ids in receipts.items():
                assignment = state["assignments"].get(assignment_id)
                for message in assignment["messages"] if assignment else ():
                    # A second read finds the mail already off "queued", so nothing to mirror.
                    if message["id"] in read_ids and message["delivery"] == "sent":
                        message["delivery"] = "read"


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


def ring(crew, message_id):
    """Best-effort doorbell: a held gate or a broken doorbell waits for the next drain.

    Only a confirmed herdr failure means the crew is unreachable and bounces the mail;
    a missing or misbehaving nudge is the harness, not the crew, so it holds instead.
    Mail is already durable, so no outcome here can lose the message.
    """
    prior = last_ring(crew)
    landed, unreachable, gate = False, None, None
    # A landed retry for this id stays content-free so the 600s drain never retypes work.
    text = None
    if not (prior and prior["id"] == message_id and prior["landed"]):
        text = next((m["text"] for m in unread(crew) if m["id"] == message_id), None)
    try:
        gate = crew.pane.nudge_block(crew)
        if gate is None:
            crew.pane.nudge(crew, text)
            landed = True
    except HERDR_ERRORS as exc:
        unreachable = str(exc)
    except Exception:
        pass
    # The one record of a ring, so drain can tell a held doorbell from one the crew already got.
    now = time.time()
    record = {"id": message_id, "at": now, "landed": landed, "gate": gate}
    if not landed:
        held_over = bool(prior) and prior["id"] == message_id and not prior["landed"]
        record["held_since"] = (prior.get("held_since") or prior["at"]) if held_over else now
        record["noticed"] = bool(held_over and prior.get("noticed"))
        if (
            gate == DRAFT_GATE
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


def drain_stamp(crew):
    return mail_dir(crew.session.directory, crew.crew_id) / ".drain"


def last_ring(crew):
    """An older session stamped a bare float; read it as a held ring for an unknown message."""
    stamp = drain_stamp(crew)
    if not stamp.exists():
        return None
    record = read_json(stamp)
    return record if isinstance(record, dict) else {"id": None, "at": record, "landed": False}


def drain(crew):
    """Retry a held doorbell for unread mail; rate-limited so a polling loop never hammers it."""
    messages = unread(crew)
    if not messages:
        return
    record = last_ring(crew)
    if record:
        landed = record["landed"] and record["id"] == messages[0]["id"]
        if time.time() - record["at"] < (DRAIN_LANDED_INTERVAL if landed else DRAIN_INTERVAL):
            return
    ring(crew, messages[0]["id"])


def deliver(crew, text, *, assignment_id=None, question_id=None, initial=False):
    check_text(text, "message")
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        if assignment["state"] == "done":
            raise CaptainError("Assignment is done; use assign with an explicit handoff.")
        last = assignment["messages"][-1] if assignment["messages"] else None
        if last and last["text"] == text:
            raise CaptainError(
                f"Message {last['id']} already carried this exact text; never auto-resend."
            )
        question = assignment["question"]
        if question and question_id != question["id"]:
            raise CaptainError("Use answer with the pending question ID.")
        if question_id and (not question or question_id != question["id"]):
            raise CaptainError("Stale question ID.")
        message = {
            "id": uuid4().hex,
            "text": text,
            "kind": "assignment" if initial else "answer" if question_id else "followup",
            "question_id": question_id,
            "delivery": "sent",
        }
        assignment_id = assignment["id"]
        prompt = (
            f"Crew name: {assignment['crew']}.\n"
            f"Assignment {assignment_id}; incarnation {assignment['incarnation_id']}; "
            f"message {message['id']}.\n"
            f"Owned paths: {', '.join(assignment['paths']) or '(none)'}. "
            f"Allowed actions: {', '.join(assignment['actions'])}.\n{text}"
        )
        enqueue(crew, prompt, message["kind"], assignment_id, message["id"])
        assignment["messages"].append(message)
        mark_sent(assignment, message)
    ring(crew, message["id"])
    return message["id"]


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
        "summary": summary[:1600],
    }


def poll(crew, ack=None, modal=True):
    from .crew import event_status

    drain(crew)
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew)
        pending, acked = assignment["pending"], False
        if ack:
            if pending and ack == pending["result"]["delivery_id"]:
                assignment["ack_seq"] = pending["seq"]
                assignment["offset"] = pending["offset"]
                assignment["native_status"] = pending["native_status"]
                assignment["pending"] = None
                assignment["last_ack"] = ack
                pending, acked = None, True
            elif ack != assignment["last_ack"]:
                raise CaptainError("Unknown delivery acknowledgement.")
        if pending:
            # It was returned once already; repeating it is what spins a captain's wait loop.
            notification = pending["result"]
            raise CaptainError(
                f"Acknowledge delivery {notification['delivery_id']} "
                f"with wait --ack before waiting again; it reported "
                f"{notification['status']}: {notification['summary']}"
            )
        seq, offset = assignment["ack_seq"], assignment["offset"]
        native_status = assignment["native_status"]
        if seq < len(assignment["notices"]):
            entry = assignment["notices"][seq]
            seq += 1
            status, summary = entry["status"], entry["summary"]
        elif assignment["state"] == "done":
            if acked:
                return result(crew, assignment, "idle")
            raise CaptainError(
                "Assignment is done and fully acknowledged; dismiss the crew or hand off."
            )
        else:
            events, offset = native_events(crew.events, offset)
            notified = [(NOTIFIED[s], e) for e in events if (s := event_status(e)) in NOTIFIED]
            if notified:
                status, event = notified[-1]
                summary = str(event.get("message") or event.get("last-assistant-message") or "")
                if status == "awaiting_approval":
                    summary = APPROVAL_SUMMARY
                    native_status = status
                elif assignment.get("idle_seen"):
                    assignment["offset"] = offset
                    return result(crew, assignment, "working")
                else:
                    # One idle per delivered message; every later crew turn also ends idle.
                    assignment["idle_seen"] = True
            else:
                # Live state rather than a consumed event, so native_status is its only dedupe.
                if not crew.record.get("pane") and crew.record.get("status") == "needs_attention":
                    status, summary = (
                        "error",
                        "Crew startup failed; inspect before explicit done/handoff.",
                    )
                elif (
                    modal
                    and crew.record.get("provider") in MODAL_PROVIDERS
                    and crew.pane.choice_modal()
                ):
                    status, summary = "awaiting_approval", APPROVAL_SUMMARY
                else:
                    status = None
                if status is None or status == native_status:
                    assignment["offset"] = offset
                    if status is None and modal:
                        # The screen is clear, so the next modal is news again.
                        assignment["native_status"] = None
                    return result(crew, assignment, "working")
                native_status = status
        response = result(crew, assignment, status, summary, uuid4().hex)
        assignment["pending"] = {
            "result": response,
            "seq": seq,
            "offset": offset,
            "native_status": native_status,
        }
        return response


def wait(crew, timeout, ack=None):
    """Poll on native signals; the one pane read a hookless provider needs is throttled."""
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
