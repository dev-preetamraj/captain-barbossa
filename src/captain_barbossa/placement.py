"""Workspace pane discovery and crew split selection."""

import sys

from . import runtime
from .crew import Crew
from .layout import HERDR_DIRECTIONS, pick_auto_split, pick_split, tab_panes
from .prompts import LABELS, choose
from .runtime import CaptainError


class Placement:
    """The captain's origin, the dashboard pane to avoid, and the active crew geometry."""

    def __init__(self, pane, current, dashboard=None):
        self.pane = pane
        self.dashboard = dashboard
        active = [crew.record for crew in Crew.members(current) if not crew.is_dismissed]
        self.crew_panes = {crew["pane"] for crew in active if crew.get("pane")}
        self.crew_tabs = []
        self.pane_to_tab = {}
        for crew in active:
            if crew.get("tab") and crew["tab"] not in self.crew_tabs:
                self.crew_tabs.append(crew["tab"])
            if crew.get("pane") and crew.get("tab"):
                self.pane_to_tab[crew["pane"]] = crew["tab"]

    def workspace_panes(self):
        """List workspace panes grouped by tab: {tab_id: (tab name, {pane_id: label})}."""
        workspace = self.pane["workspace_id"]
        tabs = runtime.herdr("tab", "list", "--workspace", workspace).get("tabs")
        listed = runtime.herdr("pane", "list", "--workspace", workspace).get("panes")
        if not isinstance(tabs, list) or not isinstance(listed, list):
            raise CaptainError("Herdr returned no tab or pane list for this workspace.")
        names = {}
        for tab in tabs:
            if isinstance(tab, dict) and isinstance(tab.get("tab_id"), str):
                number = tab.get("number")
                names[tab["tab_id"]] = tab.get("label") or (
                    f"Tab {number}" if number is not None else tab["tab_id"]
                )
        groups = {}
        for entry in listed:
            if not isinstance(entry, dict):
                continue
            pane_id, tab_id = entry.get("pane_id"), entry.get("tab_id")
            if not (isinstance(pane_id, str) and pane_id and isinstance(tab_id, str) and tab_id):
                continue
            if pane_id == self.dashboard:
                continue
            title = entry.get("label") or entry.get("terminal_title_stripped") or pane_id
            if pane_id == self.pane["pane_id"]:
                title += " (captain)"
            groups.setdefault(tab_id, (names.get(tab_id, tab_id), {}))[1][pane_id] = title
        if not groups:
            raise CaptainError("Herdr listed no panes in this workspace.")
        return groups

    def auto_split(self, direction=None, target=None, tab_id=None):
        """Pick a split from crew tabs; a None pane means fall back to a new tab.

        Try the captain tab, then this session's crew-only tabs in recruitment order.
        """
        origin = target or self.pane["pane_id"]
        captain_pane = self.pane["pane_id"]

        if target:
            geometry = tab_panes(runtime.herdr("pane", "layout", "--pane", origin), origin)
            geometry = {target: geometry[target]}
            split_pane, chosen, reason = pick_split(
                geometry, captain_pane, self.crew_panes, direction
            )
            if split_pane is None:
                choice = "new tab"
            else:
                choice = f"split {split_pane} {chosen} ({HERDR_DIRECTIONS[chosen]})"
            print(f"Auto placement: {choice}; {reason}.", file=sys.stderr)
            return chosen, split_pane, tab_id or self.pane["tab_id"], f"{choice}; {reason}"

        tabs_to_search = [self.pane["tab_id"]]
        tabs_to_search.extend(t for t in self.crew_tabs or () if t != self.pane["tab_id"])

        last_reason = None
        for search_tab in tabs_to_search:
            try:
                if search_tab == self.pane["tab_id"]:
                    geometry = tab_panes(runtime.herdr("pane", "layout", "--pane", origin), origin)
                else:
                    sample_pane = next(
                        (p for p in self.pane_to_tab or {} if self.pane_to_tab[p] == search_tab),
                        None,
                    )
                    if sample_pane is None:
                        continue
                    geometry = tab_panes(
                        runtime.herdr("pane", "layout", "--pane", sample_pane), sample_pane
                    )

                # A few rows of table: splitting it would leave half a dashboard and half
                # a crew pane, so it is never a candidate.
                geometry = {p: rect for p, rect in geometry.items() if p != self.dashboard}
                split_pane, chosen, reason = pick_auto_split(
                    geometry, captain_pane, self.crew_panes, direction
                )
                if split_pane is not None:
                    choice = f"split {split_pane} {chosen} ({HERDR_DIRECTIONS[chosen]})"
                    print(f"Auto placement: {choice}; {reason}.", file=sys.stderr)
                    return chosen, split_pane, search_tab, f"{choice}; {reason}"
                last_reason = reason
            except CaptainError:
                if search_tab == self.pane["tab_id"]:
                    raise
                continue

        choice = "new tab"
        reason = last_reason or "no feasible split"
        print(f"Auto placement: {choice}; {reason}.", file=sys.stderr)
        return None, None, tab_id or self.pane["tab_id"], f"{choice}; {reason}"

    def choose_split(self, args, placement):
        """Return (direction, split pane, tab, auto reason); a None pane with a reason means new tab."""
        if placement != "pane":
            if args.direction or args.split_pane:
                raise CaptainError("--direction and --split-pane apply only to --placement pane.")
            return None, None, None, None

        if args.split_pane == "auto":
            return self.auto_split(None if args.direction == "auto" else args.direction)
        direction = choose(
            args.direction, (*HERDR_DIRECTIONS, "auto"), "Split direction?", "--direction"
        )
        free = None if direction == "auto" else direction
        if args.split_pane == self.pane["pane_id"]:
            split_pane, tab_id = args.split_pane, self.pane["tab_id"]
        else:
            groups = self.workspace_panes()
            labels = {"auto": LABELS["auto"]}
            labels.update(
                (pane_id, title) for _, panes in groups.values() for pane_id, title in panes.items()
            )
            if args.split_pane and args.split_pane not in labels:
                raise CaptainError(
                    f"Pane {args.split_pane} is not in this workspace (closed or mistyped). "
                    f"Panes: {', '.join(labels)}. Ask the user again."
                )
            split_pane = choose(
                args.split_pane,
                tuple(labels),
                "Which pane should be split?",
                "--split-pane",
                labels=labels,
                groups=[
                    (None, ("auto",)),
                    *((name, tuple(panes)) for name, panes in groups.values()),
                ],
            )
            if split_pane == "auto":
                return self.auto_split(free)
            tab_id = next(tab for tab, (_, panes) in groups.items() if split_pane in panes)
        if direction == "auto":
            return self.auto_split(None, split_pane, tab_id)
        return direction, split_pane, tab_id, None
