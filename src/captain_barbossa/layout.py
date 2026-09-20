"""Declared tab shapes: the slot a crew takes, and the split that creates it.

A shape is a list of columns read left to right, each number the panes stacked in that
column. Nothing here measures a pane: a slot is decided from the session roster alone,
so the same roster and shape always give the same answer.
"""

from . import config
from .runtime import CaptainError

HERDR_DIRECTIONS = {"vertical": "right", "horizontal": "down"}


def _fault(columns):
    """What is wrong with a declared shape, or "" when it is usable."""
    if not isinstance(columns, list):
        return "it is not a list"
    if not columns:
        return "the list is empty"
    for index, value in enumerate(columns, 1):
        if isinstance(value, bool) or not isinstance(value, int):
            return f"column {index} is {value!r}"
        if value < 1:
            return f"column {index} is {value}"
    return ""


def shape(key):
    """The declared [placement] shape for key, as a tuple of column depths.

    A malformed shape fails the command rather than falling back: a layout that quietly
    ignores what you wrote is the bug people spend an afternoon on.
    """
    columns = config.lookup("placement", key)
    fault = _fault(columns)
    if fault:
        source = config.source("placement", key)
        where = f" from {source}" if source else ""
        raise CaptainError(
            f"[placement] {key} must be a list of whole numbers, each 1 or more, "
            f"like [1, 2, 2]. Got {columns!r}{where}: {fault}."
        )
    return tuple(columns)


def is_open(column, counts, captain):
    """Whether a column already holds a pane the next split could use."""
    return counts.get(column, 0) > 0 or (column == 1 and captain)


def next_slot(columns, counts, captain):
    """The (column, row) the next crew takes, or None when every slot is taken.

    Breadth first: a new column opens before anything stacks, so the column whose next
    row is lowest wins, and the leftmost wins a tie. On the captain's tab column 1 row 1
    is the captain, so its crew start a row lower.
    """
    slot = None
    for index, depth in enumerate(columns):
        column = index + 1
        row = counts.get(column, 0) + (2 if captain and column == 1 else 1)
        if row > depth:
            continue
        # Herdr splits right and down only, so a column can only open to the right of an
        # open one. A leftmost column emptied by dismissals cannot be rebuilt.
        if not is_open(column, counts, captain) and not (
            column > 1 and is_open(column - 1, counts, captain)
        ):
            continue
        if slot is None or row < slot[1]:
            slot = (column, row)
    return slot


def split_for(slot, panes, captain_pane):
    """(pane to split, direction) for slot, given {column: panes oldest first}.

    A column's panes are in arrival order, because a right split lands beside its source
    and a down split lands below it. So the first is the column's top pane and the last
    is its bottom, and dismissing one does not reorder the rest.
    """
    column, _ = slot
    stacked = panes.get(column) or ()
    if stacked:
        return stacked[-1], "horizontal"
    if column == 1:
        return captain_pane, "horizontal"
    # next_slot only offers a column whose left neighbour is open, and a column that is
    # open with no crew in it can only be the captain's own.
    left = panes.get(column - 1) or (captain_pane,)
    return left[0], "vertical"


def ratio_for(slot, columns, direction):
    """The share the split pane keeps, so a declared shape comes out evenly.

    Herdr halves a pane by default, which would make [1, 2, 2] land as a half and two
    quarters. Splitting off column c of n, the source spans n - c + 2 columns and has to
    keep one; a row of a column holding k is the same sum downward.
    """
    column, row = slot
    if direction == "vertical":
        return 1 / (len(columns) - column + 2)
    return 1 / (columns[column - 1] - row + 2)
