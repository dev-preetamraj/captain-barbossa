"""Crew identities, roster queries, and native lifecycle events."""

import subprocess
import time
from dataclasses import dataclass

from .memory import Session, read_events, read_json, write_json
from .pane import PANE_EMPTY_PROMPT, Pane, modal_start
from .runtime import CaptainError

WAIT_INTERVAL = 2
# Consecutive idle polls before a crew that only paused between tools counts as finished.
WAIT_POLLS = 3
WAIT_TIMEOUT = 900


def event_status(event, task=None):
    kind = event.get("hook_event_name", event.get("type"))
    if kind == "agent-turn-complete":
        # Codex also notifies for internal title-generation turns.
        messages = event.get("input-messages")
        return (
            "done"
            if task is not None and isinstance(messages, list) and messages and messages[0] == task
            else None
        )
    if kind == "Stop":
        return "done"
    if kind == "SessionStart":
        return "working"
    if kind == "PermissionRequest":
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
        return self.session.events(self.crew_id)

    @property
    def pane(self):
        return Pane(self.record["agent"])

    @classmethod
    def members(cls, current):
        return [cls(crew_id, record, current) for crew_id, record in current.meta["crew"].items()]

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

    def status(self, timeout):
        """Tail hooks; use pane detection only while no native event has arrived."""
        events = self.events
        task = self.record.get("task")
        deadline = time.monotonic() + timeout
        fallback_at = time.monotonic() + WAIT_INTERVAL * WAIT_POLLS
        cursor = events.with_suffix(".cursor")
        offset = read_json(cursor) if cursor.exists() else 0
        seen = False
        idle_polls = 0
        previous = None
        while True:
            batch, offset = read_events(events, offset)
            batch = [event for event in batch if event_status(event, task) is not None]
            seen = seen or bool(batch)
            if batch:
                # Only the newest lifecycle event describes the current state.
                event = batch[-1]
                status = event_status(event, task)
                if status != "working":
                    write_json(cursor, offset)
                    return status, event
            if not seen and time.monotonic() >= fallback_at:
                try:
                    lines = self.pane.lines()
                except (CaptainError, subprocess.TimeoutExpired, OSError):
                    lines = []
                if modal_start(lines) is not None:
                    return "blocked", None
                # ponytail: pane fallback is heuristic; native hooks are authoritative.
                idle = any(PANE_EMPTY_PROMPT.match(line) for line in lines) and not any(
                    "esc to interrupt" in line.casefold() for line in lines
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
