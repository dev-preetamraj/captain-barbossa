"""Automatic crew pane placement from the current tab's pane geometry."""

from .runtime import CaptainError

# Smallest pane (terminal cells) a native agent CLI stays usable in.
MIN_WIDTH = 60
MIN_HEIGHT = 15
HERDR_DIRECTIONS = {"vertical": "right", "horizontal": "down"}


def tab_panes(response, pane_id):
    """Parse a Herdr pane layout into {pane_id: (x, y, width, height)} for pane_id's tab."""
    layout = response.get("layout")
    panes = layout.get("panes") if isinstance(layout, dict) else None
    if not isinstance(panes, list):
        raise CaptainError("Herdr returned no pane layout for the current tab.")
    geometry = {}
    for entry in panes:
        if not isinstance(entry, dict):
            continue
        rect = entry.get("rect")
        if not (isinstance(entry.get("pane_id"), str) and isinstance(rect, dict)):
            continue
        cells = [rect.get(key) for key in ("x", "y", "width", "height")]
        if all(isinstance(cell, int) and cell >= 0 for cell in cells):
            geometry[entry["pane_id"]] = tuple(cells)
    if pane_id not in geometry:
        raise CaptainError(f"Herdr's layout for the current tab does not include pane {pane_id}.")
    return geometry


def half(rect, direction):
    """Size of each pane after an even split; the new pane never gains the divider cell."""
    _, _, width, height = rect
    return (width // 2, height) if direction == "vertical" else (width, height // 2)


def balance(width, height):
    """Area of the resulting pane, discounted by how far it is from a square on screen.

    Terminal cells are about twice as tall as wide, so a pane looks square when width is
    twice its height. A wide sliver or a tall sliver scores low either way, which is what
    alternates the split direction as a tab fills up.
    """
    aspect = width / (2 * height)
    return width * height * min(aspect, 1 / aspect)


def pick_split(geometry, captain, crew_panes, direction=None):
    """Choose (pane_id, direction, reason); pane_id is None when the tab is too crowded.

    Every feasible split keeps both halves at least MIN_WIDTH x MIN_HEIGHT. Among those, the
    split whose halves stay largest and squarest wins, then panes without crew, then panes
    nearest the captain. Splitting the captain's own pane down is the last resort before a
    new tab: it shrinks the pane the user is typing in.
    """
    directions = (direction,) if direction else tuple(HERDR_DIRECTIONS)
    anchor = center(geometry[captain]) if captain in geometry else None
    candidates = []
    for pane_id, rect in geometry.items():
        distance = 0
        if anchor:
            px, py = center(rect)
            distance = abs(px - anchor[0]) / 2 + abs(py - anchor[1])
        for option in directions:
            width, height = half(rect, option)
            if width < MIN_WIDTH or height < MIN_HEIGHT:
                continue
            last_resort = pane_id == captain and option == "horizontal"
            key = (last_resort, -balance(width, height), pane_id in crew_panes, distance)
            candidates.append((key, pane_id, option, width, height))
    if not candidates:
        return (
            None,
            None,
            (
                f"every pane in this tab would drop below {MIN_WIDTH}x{MIN_HEIGHT} cells "
                f"when split{' ' + direction if direction else ''}"
            ),
        )
    key, pane_id, option, width, height = min(candidates, key=lambda item: item[0])
    _, _, full_width, full_height = geometry[pane_id]
    why = "largest balanced halves"
    if key[0]:
        why = "no other split fits, so the captain's pane is split down as a last resort"
    elif pane_id == captain:
        why += ", captain's own pane"
    elif pane_id not in crew_panes:
        why += ", holds no crew"
    reason = (
        f"{full_width}x{full_height} pane {pane_id} split {HERDR_DIRECTIONS[option]} "
        f"leaves {width}x{height} halves; {why}"
    )
    return pane_id, option, reason


def center(rect):
    x, y, width, height = rect
    return x + width / 2, y + height / 2
