"""Explicit assignment state and acknowledged delivery, local to one session."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .memory import lock, read_json, write_json
from .models import HOOK_DELIVERED, HOOKLESS
from .pane import DRAFT_EXPIRY, DRAFT_GATE
from .runtime import HERDR_ERRORS, CaptainError, check_text

ACTIONS = ("read", "search", "edit", "test", "build", "format", "commit", "version", "delete")
# A composer read this soon after a ring that landed may be showing our own echo mid-paint,
# so a draft verdict inside the window is "don't know" and may not start a hold.
# From munder-difflin's ECHO_GRACE_MS (terminalPool.ts:422).
ECHO_GRACE = 2
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
        # Name the live one. A crew's launch-bound CAPTAIN_ASSIGNMENT goes stale the
        # moment the captain hands off, and a bare refusal left it unable to discover
        # what replaced it: it could not report, and `ask` was stale too.
        raise CaptainError(
            f"Stale assignment ID {assignment_id}; this crew's live assignment is {key}. "
            f"Retry with --assignment {key}."
        )
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
        if previous:
            old = state["assignments"][previous]
            if not handoff and old["incarnation_id"] != crew.record["incarnation_id"]:
                # The name was reused after its former incarnation was dismissed; that
                # incarnation released the name, so a plain recruit starts fresh.
                previous = None
            elif old["state"] != "done" or handoff != previous:
                raise CaptainError(
                    "Reassignment requires done with report and --handoff ASSIGNMENT_ID."
                )
            elif old["pending"] or old["ack_seq"] != len(old["notices"]):
                raise CaptainError(
                    "Acknowledge the previous assignment notifications before handoff."
                )
        if handoff and not previous:
            raise CaptainError("No previous assignment matches this handoff.")
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
    if args.command == "done":
        # `done` is the crew's last action, so it always runs BEFORE the Stop hook: a
        # refusal here pre-empts Stop delivery every time, which is why a follow-up that
        # arrived mid-turn only ever surfaced as "go and run inbox". Deliver it instead.
        deliver_before_done(crew)
    with checkpoint(crew.session.directory) as state:
        # `ask` is the crew's only way to reach the captain, so it binds to whatever
        # assignment is live rather than to the id the crew was launched with. A crew
        # holding a retired id could otherwise neither finish nor ask why, and the Stop
        # hook kept re-blocking it for the unfinished assignment it could not escape.
        # Every other command keeps the explicit id, because a report must not silently
        # attach itself to an assignment the captain replaced.
        assignment = active(
            state, crew, None if args.command == "ask" else assignment_id, incarnation
        )
        if args.command == "check":
            check_action(assignment, project, args.action, args.path)
            return {"assignment_id": assignment["id"], "allowed": True}
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
            return {"assignment_id": assignment["id"], "question_id": question["id"]}
        check_text(args.report, "report")
        if assignment["question"]:
            raise CaptainError("Answer the pending question before done.")
        if unread(crew):
            # Only reachable when mail landed between deliver_before_done and this lock;
            # that message is delivered by the next attempt rather than named here.
            raise CaptainError("Mail arrived while this report was being filed; retry done.")
        assignment["report"] = args.report
        assignment["state"] = "done"
        notice(assignment, "done", args.report)
        return {"assignment_id": assignment["id"], "status": "done"}


def deliver_before_done(crew):
    """Hand over mail that arrived mid-turn, as the refusal itself, then refuse.

    The Stop hook cannot cover this case: a crew that follows its instructions calls
    `done` as its last action, so `done` always runs first and a refusal here pre-empts
    the boundary Stop would have delivered at. Carrying the body in the refusal keeps the
    one-channel rule - the crew is never told to go and fetch anything - and stamps the
    same receipt the hook would have. Stop stays as the backstop for a crew that stops
    without reporting at all.

    Called outside the protocol lock, because the receipt takes it.
    """
    messages = unread(crew)
    if not messages:
        return
    body = render(crew.session.directory, messages)
    receipt(crew.session.directory, crew.crew_id, [message["id"] for message in messages])
    raise CaptainError(
        f"{body}\n\nThat mail arrived before this report. Act on it, then run done again."
    )


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


def receipt(session_directory, crew_id, ids):
    """Stamp a read receipt on each named message. The one writer of `read`, for the crew's
    own `inbox` and for a delivery hook alike, so neither can record it differently."""
    directory = mail_dir(session_directory, crew_id)
    receipts = {}
    with lock(session_directory / "protocol.lock"):
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
        with checkpoint(session_directory) as state:
            for assignment_id, read_ids in receipts.items():
                assignment = state["assignments"].get(assignment_id)
                for message in assignment["messages"] if assignment else ():
                    # A second read finds the mail already off "queued", so nothing to mirror.
                    if message["id"] in read_ids and message["delivery"] == "sent":
                        message["delivery"] = "read"


def mark_read(crew, ids):
    """The crew's own read writes the receipt; a bounced message is never resurrected."""
    receipt(crew.session.directory, crew.crew_id, ids)


def render(session_directory, messages):
    """The one rendering of mail for a model, or None when there is none.

    Used by the delivery hook, by `inbox`, and by the ring that is a hookless crew's only
    channel, so none of the three can show the crew a different thing.
    """
    if not messages:
        return None
    state = read_json(session_directory / "protocol.json")
    # Queued mail belongs to the crew's one active assignment; the first names it.
    assignment = state["assignments"].get(messages[0].get("assignment_id"))
    header = f"{identity(assignment)}\n\n" if assignment else ""
    bodies = "\n\n".join(message["text"] for message in messages)
    return f"Mail from the captain:\n\n{header}{bodies}"


def identity(assignment):
    """The crew's standing facts, rendered from state at read time.

    Never stored in a message body: it is the same four lines for every message of an
    assignment, and pasting it into each one re-sent it on every follow-up.
    """
    return (
        f"Crew name: {assignment['crew']}.\n"
        f"Assignment {assignment['id']}; incarnation {assignment['incarnation_id']}.\n"
        f"Owned paths: {', '.join(assignment['paths']) or '(none)'}. "
        f"Allowed actions: {', '.join(assignment['actions'])}."
    )


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


def stale_draft(prior, draft):
    """Whether this same draft has already held the doorbell for DRAFT_EXPIRY.

    A hold that never ends is its own failure: before this, one unreadable composer kept a
    crew's mail for the rest of the session and a human had to clear it by hand.
    """
    if not prior or prior.get("draft") != draft or prior.get("draft_since") is None:
        return False
    return time.time() - prior["draft_since"] >= DRAFT_EXPIRY


def echoing(prior):
    """Whether a ring landed so recently that the composer may still be painting it."""
    return bool(prior) and prior.get("landed") and time.time() - prior["at"] < ECHO_GRACE


def ring(crew, message_id):
    """Best-effort doorbell: a held gate or a broken doorbell waits for the next drain.

    Only a confirmed herdr failure means the crew is unreachable and bounces the mail;
    a missing or misbehaving nudge is the harness, not the crew, so it holds instead.
    Mail is already durable, so no outcome here can lose the message.
    """
    prior = last_ring(crew)
    provider = crew.record.get("provider")
    landed, unreachable, gate, draft = False, None, None, None
    # A hook-delivered crew is handed its mail by its own hook, so every ring to it is the
    # bare wake line: nothing to retype, nothing to branch on, safe to repeat or to drop.
    # A landed retry stays content-free too, so the 600s drain never retypes work.
    text = None
    if provider not in HOOK_DELIVERED and not (
        prior and prior["id"] == message_id and prior["landed"]
    ):
        # The ring is this crew's only delivery channel, so it carries the rendered body,
        # identity and all: it may act on the ring without ever running `inbox`.
        message = next((m for m in unread(crew) if m["id"] == message_id), None)
        text = render(crew.session.directory, [message] if message else [])
    try:
        pane = crew.pane
        gate = pane.nudge_block(crew)
        if gate == DRAFT_GATE and echoing(prior):
            # Our own ring, still painting. A composer read inside the grace window proves
            # nothing, and a draft invented from it would start a hold clock against a crew
            # that already has its doorbell. Leave the prior stamp alone and let drain retry.
            return
        if gate == DRAFT_GATE:
            draft = pane.draft(provider)
            if stale_draft(prior, draft):
                # An expired draft stops holding, but the body never joins a human's
                # half-written sentence: ring the inbox line alone, which is a valid ring.
                gate, text = None, None
        if gate is None:
            pane.nudge(crew, text)
            landed = True
    except HERDR_ERRORS as exc:
        unreachable = str(exc)
    except Exception:
        pass
    # The one record of a ring, so drain can tell a held doorbell from one the crew already got.
    now = time.time()
    record = {"id": message_id, "at": now, "landed": landed, "gate": gate}
    if draft is not None:
        # Age the draft itself, not the message: a human who edits their line starts the clock
        # over, and only an unchanged one expires.
        carried = prior.get("draft_since") if prior and prior.get("draft") == draft else None
        record["draft"], record["draft_since"] = draft, carried or now
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
        # The body is the captain's text alone; identity() renders the standing facts at
        # read time, once per delivery, for the hook and for `inbox` alike.
        enqueue(crew, text, message["kind"], assignment_id, message["id"])
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
                    # One approval reaches us twice: a PermissionRequest hook and a
                    # permission_prompt Notification, usually in separate polls. Until
                    # something proves the prompt cleared, the second is the same news.
                    if native_status == status:
                        assignment["offset"] = offset
                        return result(crew, assignment, "working")
                    summary = APPROVAL_SUMMARY
                    native_status = status
                elif assignment.get("idle_seen"):
                    assignment["offset"] = offset
                    # A turn that ran proves any approval cleared; the next one is news.
                    assignment["native_status"] = None
                    return result(crew, assignment, "working")
                else:
                    # One idle per delivered message; every later crew turn also ends idle.
                    assignment["idle_seen"] = True
                    native_status = None
            else:
                # Live state rather than a consumed event, so native_status is its only dedupe.
                read_pane = modal and crew.record.get("provider") in MODAL_PROVIDERS
                if not crew.record.get("pane") and crew.record.get("status") == "needs_attention":
                    status, summary = (
                        "error",
                        "Crew startup failed; inspect before explicit done/handoff.",
                    )
                elif read_pane and crew.pane.choice_modal():
                    status, summary = "awaiting_approval", APPROVAL_SUMMARY
                else:
                    status = None
                if status is None or status == native_status:
                    assignment["offset"] = offset
                    if status is None and read_pane:
                        # A pane we LOOKED at and found clear means the next modal is news.
                        # Only then: a provider whose approvals arrive as events is never
                        # looked at here, and clearing on that absence wiped the dedupe on
                        # every poll, so one approval surfaced once per event reporting it.
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
    """Poll on native signals; the one pane read a hookless provider needs is throttled.

    A wait that reaches its deadline with nothing to report returns `timeout`, and a
    captain reading that is a model turn spent to learn nothing. `timeout` therefore
    exists only for a caller that asked for a bounded look (`--timeout`, including 0 for
    "report what has already arrived"); the configured default is a full day, long enough
    that quiet never wakes anyone. This is the no-daemon equivalent of a watcher process:
    the captain already backgrounds this, so keeping it quiet costs nothing, while
    returning early costs a whole context.
    """
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
