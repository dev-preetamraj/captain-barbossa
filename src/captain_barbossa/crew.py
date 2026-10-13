"""Crew identities, roster queries, and native lifecycle events."""

import re
import time
from dataclasses import dataclass

from .events import event_incarnation, read_events
from .models import HOOKLESS
from .pane import INTERRUPT_MARKER, PANE_EMPTY_PROMPT, Pane, modal_start
from .runtime import HERDR_ERRORS, CaptainError
from .sessions import Session, read_session
from .store import read_cursor, read_json, write_json

WAIT_INTERVAL = 2
# Consecutive idle polls before a crew that only paused between tools counts as finished.
WAIT_POLLS = 3
# Codex's own title-generation turn notifies like any other; its prompt names what it is.
CODEX_TITLE_PROMPT = "single-line task title"
STATUS_PREFIX = re.compile(r"^(working|paused-until|blocked|needs-decision|done|failed)(?=:|$)")


@dataclass(frozen=True)
class CrewState:
    state: str
    source: str
    detail: str

    def __str__(self):
        return f"state:{self.state} source:{self.source} {' '.join(self.detail.split())}"


def declared_state(text, source):
    """Keep an unrecognized declaration visible instead of silently skipping it."""
    text = str(text).strip()
    match = STATUS_PREFIX.match(text)
    if match:
        return CrewState(match[1], source, text[match.end() :].lstrip(": "))
    return CrewState("unknown", source, f"unknown status prefix: {text}")


def event_status(event):
    kind = event.get("hook_event_name", event.get("type"))
    if kind == "agent-turn-complete":
        # agent-turn-complete is the only event Codex sends, so every turn but its internal
        # title generation counts as the crew's. Matching the submitted text instead dropped
        # any turn the composer reflowed or the user retyped, and nothing else reported it.
        first = next(iter(event.get("input-messages") or []), None)
        return None if isinstance(first, str) and CODEX_TITLE_PROMPT in first else "done"
    if kind == "Stop":
        return "done"
    if kind == "SessionStart":
        return "working"
    if kind == "PermissionRequest" or (
        kind == "PreToolUse" and event.get("approval", {}).get("verdict") == "escalate"
    ):
        return "blocked"
    if kind == "Notification" and isinstance(event.get("notification_type"), str):
        return {"idle_prompt": "idle", "permission_prompt": "blocked"}.get(
            event.get("notification_type")
        )
    return None


@dataclass
class Crew:
    """A roster record and the session paths and terminal that belong to it."""

    crew_id: str
    record: dict
    session: Session
    # Legacy records without a name echo the caller's spelling.
    requested: str | None = None

    @property
    def display_name(self):
        return self.record.get("name", self.crew_id if self.requested is None else self.requested)

    @property
    def is_dismissed(self):
        return self.record.get("status") == "dismissed"

    @property
    def events(self):
        incarnation = self.record.get("incarnation_id")
        return self.session.events(f"{self.crew_id}-{incarnation}" if incarnation else self.crew_id)

    @property
    def cursor(self):
        return self.events.with_suffix(".cursor")

    @property
    def report_cursor(self):
        return self.events.with_suffix(".reports")

    @property
    def pane(self):
        return Pane(self.record["agent"])

    def current_state(self):
        """Read current evidence, independent of the wait consumer's event cursor."""
        reasons = []
        incarnation = self.record.get("incarnation_id")
        try:
            event_incarnation(self.events)
        except (CaptainError, OSError, ValueError) as exc:
            return CrewState("unknown", "none", f"cannot verify incarnation: {exc}")
        try:
            state = read_json(self.session.directory / "protocol.json")
            assignment = state["assignments"].get(state["active"].get(self.crew_id))
        except (CaptainError, OSError, KeyError, ValueError):
            assignment = None
            reasons.append("mail state unreadable")
        if assignment and assignment.get("incarnation_id") != incarnation:
            return CrewState("unknown", "none", "stale incarnation")
        pane_status = None
        try:
            pane_status = self.pane.agent_status()
            if pane_status is None:
                reasons.append("pane missing or agent no longer registered")
        except HERDR_ERRORS:
            reasons.append("pane unreadable")
        try:
            batch, _ = read_events(self.events, 0, incarnation=incarnation)
            if not self.events.exists():
                reasons.append("no events file")
            elif not batch:
                reasons.append("no valid events (stale incarnation or sequence, or empty log)")
        except (CaptainError, OSError, ValueError):
            batch = []
            reasons.append("events unreadable")
        for event in reversed(batch):
            if pane_status is None:
                break  # Historical hooks cannot prove a missing agent is still blocked or working.
            if "status" in event:
                return declared_state(event["status"], "hook")
            kind = event.get("hook_event_name", event.get("type"))
            if event_status(event) == "blocked":
                return CrewState("blocked", "hook", str(kind))
            if kind in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
                return CrewState("working", "hook", kind)
            status = event_status(event)
            if status in ("working", "blocked"):
                return CrewState(status, "hook", str(kind))
            if status in ("done", "idle"):
                reasons.append("hook reports inactivity, not assignment completion")
                break
        try:
            status = pane_status
            if status in ("working", "blocked"):
                return CrewState(status, "pane", "native agent status")
            if status is not None and modal_start(self.pane.lines()) is not None:
                return CrewState("blocked", "pane", "approval prompt")
            reasons.append(f"pane state {status or 'missing'} does not prove assignment state")
        except HERDR_ERRORS:
            reasons.append("pane unreadable")
        if assignment:
            if assignment.get("state") == "done":
                return CrewState("done", "mail", assignment.get("report") or "assignment completed")
            if assignment.get("question"):
                return CrewState("needs-decision", "mail", "assignment has an unanswered question")
        reasons.append("no explicit assignment outcome or pending question")
        return CrewState("unknown", "none", "; ".join(reasons))

    @classmethod
    def members(cls, current):
        return [cls(crew_id, record, current) for crew_id, record in current.meta["crew"].items()]

    @classmethod
    def for_args(cls, args, pane, project):
        """The session named by args and the crew it names, for a command that takes a name."""
        current = read_session(project, args.session, pane)
        return current, cls.resolve(current, args.name)

    @classmethod
    def resolve(cls, current, requested):
        meta = current.meta
        name = requested.strip().casefold()
        matches = [
            crew_id
            for crew_id, crew in meta["crew"].items()
            if name
            in {crew_id.casefold(), crew.get("name", crew_id).casefold(), crew["agent"].casefold()}
        ]
        if not matches:
            available = ", ".join(crew.get("name", key) for key, crew in meta["crew"].items())
            raise CaptainError(
                f"No crew named '{requested}' in this session. Available crew: {available or 'none'}."
            )
        if len(matches) > 1:
            targets = ", ".join(meta["crew"][crew_id]["agent"] for crew_id in matches)
            raise CaptainError(
                f"Crew name '{requested}' is ambiguous. Use an agent name: {targets}."
            )
        return cls(matches[0], meta["crew"][matches[0]], current, requested)

    @classmethod
    def name_reserved(cls, current, name):
        return (
            name in current.meta["crew"]
            and not cls(name, current.meta["crew"][name], current).is_dismissed
        )

    @classmethod
    def tab_label(cls, current, tab_id):
        """Build a tab label showing all active crew: 'Elizabeth' or 'Elizabeth +2'."""
        crew_in_tab = [
            crew.display_name
            for crew in cls.members(current)
            if crew.record.get("tab") == tab_id and not crew.is_dismissed
        ]
        if not crew_in_tab:
            return None
        if len(crew_in_tab) == 1:
            return crew_in_tab[0]
        return f"{crew_in_tab[0]} +{len(crew_in_tab) - 1}"

    @property
    def reports(self):
        """Reports recorded under any of the crew's names, in the order they were written."""
        path = self.session.graph
        names = (self.display_name, self.record.get("id"), self.record["agent"])
        if not path.exists():
            return []
        graph = read_json(path)
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        wanted = {name.casefold() for name in names if name}
        return [
            labels.get(link["target"], "")
            for link in graph["links"]
            if link.get("relation") == "report"
            and labels.get(link["source"], "").casefold() in wanted
        ]

    def pi_status(self, timeout):
        """Poll Herdr's own agent status, which is all pi offers: it installs no hooks.

        Idle is debounced exactly like the pane fallback: a task that has not started yet
        also reads idle, and one between tools reads idle for a moment.
        """
        deadline = time.monotonic() + timeout
        fallback_at = time.monotonic() + WAIT_INTERVAL * WAIT_POLLS
        idle_polls = 0
        while True:
            if time.monotonic() >= fallback_at:
                try:
                    status = self.pane.agent_status()
                except HERDR_ERRORS:
                    status = None
                # Herdr sees pi's approval prompts even though pi fires no hooks.
                if status in ("done", "blocked"):
                    return status
                idle_polls = idle_polls + 1 if status == "idle" else 0
                if idle_polls >= WAIT_POLLS:
                    return "idle"
            if time.monotonic() >= deadline:
                return None
            time.sleep(WAIT_INTERVAL)

    def status(self, timeout):
        """Tail hooks; use pane detection only while no native event has arrived."""
        if self.record.get("provider") in HOOKLESS:
            return self.pi_status(timeout), None
        events = self.events
        deadline = time.monotonic() + timeout
        fallback_at = time.monotonic() + WAIT_INTERVAL * WAIT_POLLS
        cursor = self.cursor
        offset = read_cursor(cursor)
        seen = False
        idle_polls = 0
        previous = None
        while True:
            batch, offset = read_events(events, offset)
            batch = [event for event in batch if event_status(event) is not None]
            seen = seen or bool(batch)
            if batch:
                # Only the newest lifecycle event describes the current state.
                event = batch[-1]
                status = event_status(event)
                if status != "working":
                    write_json(cursor, offset)
                    return status, event
            if not seen and time.monotonic() >= fallback_at:
                try:
                    lines = self.pane.lines()
                except HERDR_ERRORS:
                    lines = []
                if modal_start(lines) is not None:
                    return "blocked", None
                # ponytail: pane fallback is heuristic; native hooks are authoritative.
                idle = any(PANE_EMPTY_PROMPT.match(line) for line in lines) and not any(
                    INTERRUPT_MARKER in line.casefold() for line in lines
                )
                idle_polls = idle_polls + 1 if idle and lines == previous else int(idle)
                previous = lines
                if idle_polls >= WAIT_POLLS:
                    return "idle", None
            if time.monotonic() >= deadline:
                if events.exists():
                    write_json(cursor, offset)
                return None, None
            time.sleep(WAIT_INTERVAL)
