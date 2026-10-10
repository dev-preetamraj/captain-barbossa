"""Workspace pane discovery, and the slot a crew takes in a declared tab shape."""

import sys
from dataclasses import dataclass

from . import runtime
from .crew import Crew
from .layout import (
    HERDR_DIRECTIONS,
    SHRINK_DIRECTIONS,
    next_slot,
    re_even,
    shape,
    split_for,
    widen_split,
)
from .prompts import LABELS, choose
from .runtime import HERDR_ERRORS, CaptainError


@dataclass(frozen=True)
class Spot:
    """Where a crew opens. A None pane with a reason means a new tab."""

    direction: str | None = None
    pane: str | None = None
    tab: str | None = None
    reason: str | None = None
    column: int | None = None
    row: int | None = None
    ratio: float | None = None


class Placement:
    """The captain's origin and this session's roster, which is all a slot needs."""

    def __init__(self, pane, current, dashboard=None):
        self.pane = pane
        self.dashboard = dashboard
        self.active = [crew.record for crew in Crew.members(current) if not crew.is_dismissed]
        self.crew_tabs = []
        for crew in self.active:
            if crew.get("tab") and crew["tab"] not in self.crew_tabs:
                self.crew_tabs.append(crew["tab"])

    def unmapped(self, tab):
        """Whether tab holds a crew recruited before slots were recorded at all.

        Such a record predates this version, so its column is not None but missing, and
        the grid would read the tab as empty and split the captain's pane over live crew.
        A crew placed by hand is a None column, which is a slot the grid knows to skip.
        """
        return any(
            "column" not in crew
            for crew in self.active
            if crew.get("tab") == tab and crew.get("pane")
        )

    def occupancy(self, tab):
        """({column: crew in it}, {column: panes oldest first}) for one tab, from the roster.

        A crew placed by hand holds no column, so it is counted nowhere and split never:
        one hand placement would otherwise shift every slot after it.
        """
        rows = {}
        for crew in self.active:
            if crew.get("tab") != tab or not crew.get("pane"):
                continue
            if isinstance(crew.get("column"), int) and isinstance(crew.get("row"), int):
                rows.setdefault(crew["column"], []).append((crew["row"], crew["pane"]))
        # Ordered by the row each crew was given, not by roster position: arrival runs
        # down a column, so the lowest row is its top pane whatever order the file lists.
        panes = {column: [pane for _, pane in sorted(members)] for column, members in rows.items()}
        return {column: len(members) for column, members in panes.items()}, panes

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

    def tab_order(self):
        """The captain's tab, then this session's crew tabs in recruitment order."""
        captain_tab = self.pane["tab_id"]
        return [captain_tab, *(tab for tab in self.crew_tabs if tab != captain_tab)]

    def column_stack(self, panes, column, captain):
        """One column's panes top to bottom. The captain is its own column's top pane,
        and on its own tab column 1 holds nothing else (section 1 of docs/placement.md).
        """
        stack = list(panes.get(column) or ())
        return [self.pane["pane_id"], *stack] if column == 1 and captain else stack

    def column_tops(self, panes, captain):
        """The top pane of every open column, left to right: the row a column joins."""
        return [
            self.column_stack(panes, column, captain)[0]
            for column in sorted({*panes, *((1,) if captain else ())})
        ]

    def resize(self, source, towards, amount):
        """Move one split. Herdr reports a refusal in the result, not by failing, so a
        ratio it would not reach has to be raised here; what it costs is the caller's.
        """
        result = runtime.herdr(
            "pane",
            "resize",
            "--pane",
            source,
            "--direction",
            towards,
            "--amount",
            f"{amount:.4f}",
        )
        if not result.get("resize", {}).get("changed"):
            raise CaptainError(f"Herdr would not resize pane {source} to even out this tab.")

    def even_around(self, panes, column, captain, split_pane, direction):
        """Resize what a new pane is about to squeeze, so its chain lands even.

        The chain is the whole column or row the new pane joins, not only the panes
        before the split: a dismissal can empty a column mid-row, and the crew that
        reopens it is inserted before panes already there.
        """
        if direction == "vertical":
            chain = self.column_tops(panes, captain)
        else:
            chain = self.column_stack(panes, column, captain)
        if split_pane not in chain:
            # Only a roster written before captain_tab column 1 was pinned to the captain
            # alone: its crew under the captain is what split_for hands back, and it is in
            # no chain. Leave such a tab's ratios alone rather than raise on the recruit.
            return
        after = len(chain) - chain.index(split_pane) - 1
        for source, amount in re_even(chain, after):
            self.resize(source, SHRINK_DIRECTIONS[direction], amount)
        widen = widen_split(after)
        if widen:
            self.resize(split_pane, HERDR_DIRECTIONS[direction], widen)

    def even_after_close(self, record):
        """(pane, Herdr direction, grow amount) that brings the chain a dismissed crew's
        pane leaves back to even shares. Read before the pane closes, applied after.

        The chain is the crew's column when the column outlives it, and the row of column
        tops when its pane was the column's last, since Herdr then hands the whole column
        to a sibling. A crew placed by hand holds no slot, so nothing in the grid moved.
        """
        tab, column = record.get("tab"), record.get("column")
        if not record.get("pane") or not tab or not isinstance(column, int):
            return []
        captain = tab == self.pane["tab_id"]
        _, panes = self.occupancy(tab)
        stack = self.column_stack(panes, column, captain)
        if len(stack) > 1:
            chain, direction = stack, "horizontal"
        else:
            chain, direction = self.column_tops(panes, captain), "vertical"
        if record["pane"] not in chain:
            return []
        survivors = [pane for pane in chain if pane != record["pane"]]
        position = chain.index(record["pane"]) + 1
        # A dismissal is a recruit run backwards: Herdr hands the gap to the panes after
        # the closed one, so only the ones before it are left short.
        return [
            (pane, HERDR_DIRECTIONS[direction], amount)
            for pane, amount in re_even(survivors)[: position - 1]
        ]

    def grid_spot(self, direction=None):
        """The first free slot in the declared shapes, or a new tab when all are full.

        A crew joining a column or row that already holds others leaves Herdr to even
        every pane already there, not just the one it splits off from; this is the one
        place Placement drives Herdr rather than only reading the roster.
        """
        captain_tab = self.pane["tab_id"]
        for tab in self.tab_order():
            if self.unmapped(tab):
                continue
            captain = tab == captain_tab
            columns = shape("captain_tab" if captain else "crew_tab")
            counts, panes = self.occupancy(tab)
            slot = next_slot(columns, counts, captain)
            if slot is None:
                continue
            pane, implied = split_for(slot, panes, self.pane["pane_id"])
            chosen = direction or implied
            column, row = slot
            # A split only resizes the two panes it touches, so the new pane's own split,
            # at half, is the only ratio arithmetic can set up front; the rest of the
            # chain is evened below. A forced direction that does not match the implied
            # one leaves Herdr to halve the pane as it would have anyway, and is never
            # re-evened: the arithmetic only describes the split the shape asked for.
            ratio = 0.5 if chosen == implied else None
            if chosen == implied:
                try:
                    self.even_around(panes, column, captain, pane, chosen)
                except HERDR_ERRORS as exc:
                    # The declared slot is created however small it lands, and a chain
                    # past Herdr's resizable range is the shape's own doing. Refusing here
                    # would cost the crew and still leave the earlier resizes applied.
                    print(f"captain: tab not evened: {exc}", file=sys.stderr)
            return Spot(
                chosen,
                pane,
                tab,
                f"split {pane} {chosen} ({HERDR_DIRECTIONS[chosen]}); "
                f"column {column} row {row} of {list(columns)}",
                column,
                row,
                ratio,
            )
        return Spot(
            reason="new tab; every slot in this session's tabs is taken or unmapped",
            column=1,
            row=1,
        )

    def auto_split(self, direction=None, target=None, tab_id=None):
        """Where the next crew opens, from the roster alone; no pane is ever measured."""
        if target:
            # A pane named by hand is outside the shape, so it takes no slot and the
            # grid never splits it again. Beside it unless the flag says otherwise.
            chosen = direction or "vertical"
            spot = Spot(
                chosen,
                target,
                tab_id or self.pane["tab_id"],
                f"split {target} {chosen} ({HERDR_DIRECTIONS[chosen]}); pane named by hand",
            )
        else:
            spot = self.grid_spot(direction)
        print(f"Auto placement: {spot.reason}.", file=sys.stderr)
        return spot

    def choose_split(self, args, placement):
        """The Spot a crew opens in; a None pane with a reason means a new tab."""
        if placement != "pane":
            if args.direction or args.split_pane:
                raise CaptainError("--direction and --split-pane apply only to --placement pane.")
            return Spot()

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
        return Spot(direction, split_pane, tab_id)
