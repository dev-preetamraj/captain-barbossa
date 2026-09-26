"""Explicit assignment state and acknowledged delivery, local to one session."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .memory import lock, read_json, write_json
from .runtime import CaptainError, check_text

ACTIONS = ("read", "search", "edit", "test", "build", "format", "commit", "version", "delete")


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
        if any(message["delivery"] in ("pending", "unknown") for message in assignment["messages"]):
            raise CaptainError("Resolve uncertain prompt delivery before done.")
        assignment["report"] = args.report
        assignment["state"] = "done"
        notice(assignment, "done", args.report)
        return {"assignment_id": assignment_id, "status": "done"}


def deliver(crew, text, *, assignment_id=None, question_id=None, initial=False):
    check_text(text, "message")
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        if assignment["state"] == "done":
            raise CaptainError("Assignment is done; use assign with an explicit handoff.")
        if any(message["delivery"] in ("pending", "unknown") for message in assignment["messages"]):
            raise CaptainError("Delivery is unknown; inspect and resolve it, never auto-resend.")
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
            "delivery": "pending",
        }
        assignment["messages"].append(message)
        assignment_id = assignment["id"]
        prompt = (
            f"Crew name: {assignment['crew']}.\n"
            f"Assignment {assignment_id}; incarnation {assignment['incarnation_id']}; "
            f"message {message['id']}.\n"
            f"Owned paths: {', '.join(assignment['paths']) or '(none)'}. "
            f"Allowed actions: {', '.join(assignment['actions'])}.\n{text}"
        )
    try:
        crew.pane.submit_task(prompt, crew.record.get("provider"), attempts=1)
    except Exception:
        record_delivery(crew, assignment_id, message["id"], "unknown")
        raise
    record_delivery(crew, assignment_id, message["id"], "sent")
    return message["id"]


def record_delivery(crew, assignment_id, message_id, outcome):
    """A late send result cannot overwrite a resolution, another send, or a new assignment."""
    with checkpoint(crew.session.directory) as state:
        assignment = state["assignments"][assignment_id]
        message = next(m for m in assignment["messages"] if m["id"] == message_id)
        if message["delivery"] != "pending":
            return
        message["delivery"] = outcome
        question = assignment["question"]
        if outcome == "unknown":
            notice(
                assignment, "delivery_unknown", f"Inspect message {message_id} before resolving."
            )
        elif question and question["id"] == message["question_id"]:
            assignment["question"] = None
            assignment["state"] = "working"


def resolve_delivery(crew, assignment_id, message_id, outcome):
    """A captain records observed delivery; this command never sends terminal input."""
    if outcome not in ("sent", "cancelled"):
        raise CaptainError("Resolution must be sent or cancelled after inspection.")
    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew, assignment_id)
        message = next((m for m in assignment["messages"] if m["id"] == message_id), None)
        if not message or message["delivery"] not in ("pending", "unknown"):
            raise CaptainError("No uncertain delivery with that message ID.")
        if outcome == "sent":
            message["delivery"] = "sent"
            question = assignment["question"]
            if question and question["id"] == message["question_id"]:
                assignment["question"] = None
                assignment["state"] = "working"
        else:
            # Keep the failed message; cancellation permits an explicit next action, not a retry.
            message["delivery"] = "cancelled"


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


def poll(crew, ack=None):
    from .crew import event_status

    with checkpoint(crew.session.directory) as state:
        assignment = active(state, crew)
        pending = assignment["pending"]
        if ack:
            if pending and ack == pending["result"]["delivery_id"]:
                assignment["ack_seq"] = pending["seq"]
                assignment["offset"] = pending["offset"]
                assignment["native_status"] = pending["native_status"]
                assignment["pending"] = None
                assignment["last_ack"] = ack
                pending = None
            elif ack != assignment["last_ack"]:
                raise CaptainError("Unknown delivery acknowledgement.")
        if pending:
            return pending["result"]
        seq, offset = assignment["ack_seq"], assignment["offset"]
        native_status = assignment["native_status"]
        if seq < len(assignment["notices"]):
            entry = assignment["notices"][seq]
            seq += 1
            status, summary = entry["status"], entry["summary"]
            if status == "delivery_unknown":
                native_status = status
        elif any(m["delivery"] in ("pending", "unknown") for m in assignment["messages"]):
            status, summary = (
                "delivery_unknown",
                "Inspect pending terminal delivery before resolving.",
            )
            if native_status == status:
                return result(crew, assignment, "working")
            native_status = status
        elif assignment["state"] == "done":
            return result(crew, assignment, "idle")
        else:
            events, offset = native_events(crew.events, offset)
            meaningful = [(event_status(e), e) for e in events if event_status(e)]
            if meaningful:
                status, event = meaningful[-1]
                status = {"done": "idle", "blocked": "awaiting_approval"}.get(status, status)
                summary = str(event.get("message") or event.get("last-assistant-message") or "")
                if status == "awaiting_approval":
                    summary = (
                        "Inspect native approval prompt; approval is not granted by this event."
                    )
            elif not crew.record.get("pane") and crew.record.get("status") == "needs_attention":
                status, summary = (
                    "error",
                    "Crew startup failed; inspect before explicit done/handoff.",
                )
            else:
                status = crew.pane.agent_status()
                if crew.pane.choice_modal():
                    status = "blocked"
                status = {"done": "idle", "blocked": "awaiting_approval"}.get(status, status)
                summary = "Native activity only; explicit done is required."
            if (
                status not in ("idle", "working", "awaiting_approval", "error")
                or status == native_status
            ):
                # No notification was created; only ignored/duplicate native input advances.
                assignment["offset"] = offset
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
    deadline = time.monotonic() + timeout
    while True:
        response = poll(crew, ack)
        ack = None
        if response["delivery_id"] or response["status"] == "idle":
            return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            response["status"] = "timeout"
            return response
        time.sleep(min(1, remaining))
