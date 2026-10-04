"""The crew token-usage dashboard: its Herdr pane, and the frame that pane shows."""

import re
import shlex
import shutil
import sys
import textwrap
import time

from . import config, runtime, usage
from .crew import Crew
from .models import MODELS
from .runtime import HERDR_ERRORS, CaptainError
from .sessions import Session, read_session
from .store import read_json, state_root, temp_root
from .usage import RATE_WINDOW, model_for_events, usage_for_events

# Label, base width, right-aligned. A column grows to fit its content; the base keeps
# the frame steady when a number briefly shortens. See docs/dashboard-layout.md.
# CUM/NOW split the two units apart: TOK and $ accumulate over every API call the
# session has made, CTX is only the last one. Without it a cumulative count sits beside
# a percentage that resets and reads as the same number (Codex issue 3630).
# $/h names its window for the same reason: an idle crew's honest 0.00 reads as broken
# unless the header says the rate is over trailing minutes, not the session.
COLUMNS = (
    ("NAME", 7, False),
    ("AGENT", 5, False),
    ("STATUS", 8, False),
    ("CTX NOW", 10, False),
    ("CUM TOK", 6, True),
    ("CUM $", 6, True),
    (f"$/h {RATE_WINDOW // 60}m", 6, True),
)
# Column labels, TOTAL and the annotation under it always render, so H-3 people fit.
MANDATORY_ROWS = 3
# Width degradation of the table, applied cumulatively in this order. The footer is its
# own line and shortens on its own, so a long annotation never costs a column.
LADDER = ("meta", "alias", "trunc", "bar", "agent")

_DATE = re.compile(r"-\d{8}$")
_UNITS = ((10**9, "B"), (10**6, "M"), (10**3, "k"))


def _tokens(value):
    """Decimal suffix at three significant digits: 999, 1.00k, 130k, 2.70M."""
    if value is None:
        return "-"
    for scale, suffix in _UNITS:
        scaled = value / scale
        # Rounding decides the unit, so 999,999 promotes from 1000k to 1.00M.
        if scaled >= 0.9995:
            decimals = 2 if scaled < 9.995 else 1 if scaled < 99.95 else 0
            return f"{scaled:.{decimals}f}{suffix}"
    return str(value)


def _money(value):
    """USD at two decimals; `$?` when pricing is unknown, never a fabricated zero."""
    if value is None:
        return "$?"
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:.2f}"


def _rate(value):
    return "-" if value is None else _money(value)


def _ctx(pct, drops):
    if pct is None:
        return "-"
    cell = f"{pct}%".rjust(4)
    if "bar" in drops:
        return cell
    filled = min(max(int(pct / 20 + 0.5), 0), 5)
    return f"{'#' * filled}{'.' * (5 - filled)} {cell}"


def _agent(provider, model, drops):
    """`provider/model`, less a dated suffix and the provider's own model prefix."""
    model = _DATE.sub("", model or "")
    if "alias" in drops:
        model = next(
            (aliases[0] for name, aliases in MODELS.get(provider, ()) if name == model and aliases),
            model,
        )
    if model.startswith(f"{provider}-"):
        model = model[len(provider) + 1 :]
    return f"{provider}/{model or '-'}"


def _agg(values):
    """(sum of the known values, or None when none are known; whether any is unknown)."""
    known = [value for value in values if value is not None]
    return (sum(known) if known else None, len(known) != len(values))


def _live_agents():
    """One bulk `herdr agent list` call per frame, shared by the captain and crew rows."""
    try:
        return runtime.herdr("agent", "list", timeout=5).get("agents", [])
    except HERDR_ERRORS:
        return []


def _crew_status(agents):
    return {agent["name"]: agent.get("agent_status") for agent in agents if agent.get("name")}


def _captain_status(agents, record):
    """The captain carries no agent name, so match its own terminal and pane instead."""
    wanted = {record.get("terminal_id"), record.get("pane")} - {None}
    for agent in agents:
        if not agent.get("name") and wanted & {agent.get("terminal_id"), agent.get("pane_id")}:
            return agent.get("agent_status")
    return None


def _row(crew, name, status):
    """A roster row: numbers stay None when unknown so no cell fabricates a zero."""
    provider = crew.record.get("provider", "-")
    usage = usage_for_events(crew.events)
    if usage is None:
        model = crew.record.get("model") or model_for_events(crew.events)
        return (name, (provider, model), status, None, None, None, None, ("", "", ""))
    # The session file reports the model actually in use; the record only knows recruit time.
    model = usage.get("model") or crew.record.get("model")
    limit, context = usage.get("limit"), usage.get("context")
    pct = round(context / limit * 100) if limit and context is not None else None
    return (
        name,
        (provider, model),
        status,
        pct,
        usage.get("tokens"),
        usage.get("cost"),
        usage.get("rate"),
        ("", "", ""),
    )


def _safe_row(crew, name, status):
    """A broken record or transcript degrades to dashes rather than killing the frame."""
    try:
        return _row(crew, name, status)
    except Exception:
        return (name, ("-", None), status, None, None, None, None, ("", "", ""))


def _retired(events):
    """A retired incarnation's cumulative (tokens, cost). It keeps no row and no rate."""
    try:
        usage = usage_for_events(events) or {}
        return (usage.get("tokens"), usage.get("cost"))
    except Exception:
        return (None, None)


def _orphaned(events_dir, claimed):
    """Retired usage for incarnations no live roster record claims.

    A dismissed crew's name returns to the pool and the next recruit overwrites its
    roster record (meta["crew"][name]), but the old incarnation's events file stays on
    disk under its own uuid; count it so TOTAL never shrinks when a name is reused.
    """
    try:
        paths = sorted(events_dir.glob("*.jsonl"))
    except OSError:
        return []
    return [_retired(path) for path in paths if path.stem not in claimed and path.stem != "captain"]


def _captain_row(current, agents):
    """The CAPTAIN row, or None when this session never recorded a captain.

    The captain's own hooks write events("captain"); before they land there is no
    transcript, so usage_for_events finds nothing and the row renders dashes.
    """
    try:
        record = read_json(current.directory / "captain.json")
    except CaptainError:
        return None
    if not isinstance(record, dict):
        return None
    crew = Crew("captain", {**record, "name": "CAPTAIN"}, current)
    return _safe_row(crew, "CAPTAIN", _captain_status(agents, record) or "-")


def _fresh(current):
    """Re-read the roster: meta freezes when the Session is built, but crew keep arriving."""
    try:
        return Session(current.directory, read_json(current.meta_path))
    except CaptainError:
        return current


def _header(current, crew_count):
    return f"session {current.meta.get('id', '-')}  crew {crew_count}  {time.strftime('%H:%M:%S')}"


def _roster(current, agents):
    """(current rows, dismissed crew's (tokens, cost), current crew count)."""
    live = _crew_status(agents)
    rows, retired, claimed = [], [], set()
    for crew in Crew.members(current):
        claimed.add(crew.events.stem)
        try:
            name = crew.display_name
        except Exception:
            name = crew.crew_id
        if crew.is_dismissed:
            retired.append(_retired(crew.events))
            continue
        status = live.get(crew.record.get("agent")) or crew.record.get("status") or "-"
        rows.append(_safe_row(crew, name, status))
    retired.extend(_orphaned(current.directory / "events", claimed))
    crew_count = len(rows)
    captain = _captain_row(current, agents)
    if captain:
        rows.insert(0, captain)
    return rows, retired, crew_count


def _total(rows, retired):
    """The TOTAL row. Cumulative columns include retired crew; the rate never does."""
    tokens, tokens_gap = _agg([row[4] for row in rows] + [item[0] for item in retired])
    cost, cost_gap = _agg([row[5] for row in rows] + [item[1] for item in retired])
    rate, rate_gap = _agg([row[6] for row in rows])
    marks = tuple("+?" if gap else "" for gap in (tokens_gap, cost_gap, rate_gap))
    return ("TOTAL", "-", "-", None, tokens, cost, rate, marks), any(marks)


def _more(hidden):
    """One row standing in for the current crew that the pane is too short to show."""
    values, marks = [], []
    for index in (4, 5, 6):
        value, gap = _agg([row[index] for row in hidden])
        values.append(value)
        marks.append("+?" if gap else "")
    return (f"MORE({len(hidden)})", "current; resize", "-", None, *values, tuple(marks))


def _footer(retired, incomplete, width):
    """The annotation under TOTAL. It never disappears, even with nothing retired."""
    tail = "+? incomplete" if incomplete else "rounded"
    if retired:
        tokens, tokens_gap = _agg([item[0] for item in retired])
        cost, cost_gap = _agg([item[1] for item in retired])
        spend = (
            f"{_tokens(tokens)}{'+?' if tokens_gap else ''}"
            f" tok/{_money(cost)}{'+?' if cost_gap else ''}"
        )
        long = f"TOTAL includes retired({len(retired)}): {spend}; USD list est; {tail}"
        short = f"Incl retired({len(retired)}): {spend}; USD est; {tail}"
    else:
        long = f"TOTAL is session-cumulative; USD list est; {tail}"
        short = f"Cumulative; USD est; {tail}"
    return long if len(long) <= width else short


def _cells(row, drops):
    name, agent, status, pct, tokens, cost, rate, marks = row
    if isinstance(agent, tuple):
        agent = _agent(*agent, drops)
    return (
        str(name),
        agent,
        str(status),
        _ctx(pct, drops),
        _tokens(tokens) + (marks[0] if tokens is not None else ""),
        _money(cost) + (marks[1] if cost is not None else ""),
        _rate(rate) + (marks[2] if rate is not None else ""),
    )


def _table(rows, drops, width):
    columns = [column for index, column in enumerate(COLUMNS) if index != 1 or "agent" not in drops]
    # CTX is only 10 wide because of the bar; without it the percentage needs four cells,
    # and the qualifier goes with the bar rather than cost a numeric column at that width.
    columns = [
        ("CTX", 4, right) if label == "CTX NOW" and "bar" in drops else (label, base, right)
        for label, base, right in columns
    ]
    grid = [tuple(label for label, _, _ in columns)]
    for row in rows:
        cells = _cells(row, drops)
        grid.append(tuple(c for i, c in enumerate(cells) if i != 1 or "agent" not in drops))
    widths = [max(base, *(len(row[i]) for row in grid)) for i, (_, base, _) in enumerate(columns)]
    if "trunc" in drops and "agent" not in drops:
        excess = sum(widths) + len(widths) - 1 - width
        if excess > 0:
            widths[1] = max(widths[1] - excess, 4)
            grid = [(row[0], _clip(row[1], widths[1]), *row[2:]) for row in grid]
    return [
        " ".join(
            cell.rjust(size) if right else cell.ljust(size)
            for cell, size, (_, _, right) in zip(row, widths, columns)
        )
        for row in grid
    ]


def _clip(text, size):
    return text if len(text) <= size else f"{text[: size - 1]}~"


def _frame(rows, retired, header, drops, width, height):
    """One rendering at a given degradation step, fitted to the pane's height."""
    total, incomplete = _total(rows, retired)
    # An annotation too long for the pane wraps onto a second line and costs a roster
    # slot; it is never truncated, because the retired spend would go with it.
    footer = textwrap.wrap(_footer(retired, incomplete, width), max(width, 20))
    show_header = "meta" not in drops and len(rows) + MANDATORY_ROWS + len(footer) <= height
    capacity = max(height - MANDATORY_ROWS - len(footer) + 1 - int(show_header), 1)
    visible = list(rows)
    if len(visible) > capacity:
        visible = visible[: capacity - 1] + [_more(rows[capacity - 1 :])]
    lines = _table([*visible, total], drops, width) + footer
    return ([header] if show_header else []) + lines


def _notice(rows, retired):
    """Too narrow for the protected columns: state the totals rather than clip a table."""
    cells = _cells(_total(rows, retired)[0], frozenset(LADDER))
    lines = ["dashboard: widen pane", f"TOTAL {cells[4]} {cells[5]} {cells[6]}"]
    if retired:
        tokens, _ = _agg([item[0] for item in retired])
        cost, _ = _agg([item[1] for item in retired])
        lines.append(f"retired({len(retired)}): {_tokens(tokens)} tok/{_money(cost)}")
    return "\n".join(lines)


def render(current, size=None):
    """The frame as text: an optional header, the roster, TOTAL, and its annotation."""
    current = _fresh(current)
    agents = _live_agents()
    rows, retired, crew_count = _roster(current, agents)
    width, height = size or shutil.get_terminal_size((80, 24))
    # The last column stays empty so a full-width line cannot autowrap.
    width -= 1
    header = _header(current, crew_count)
    if not rows:
        return f"{header}\nNo crew."
    for step in range(len(LADDER) + 1):
        lines = _frame(rows, retired, header, frozenset(LADDER[:step]), width, height)
        if max(len(line) for line in lines) <= width:
            return "\n".join(lines)
    return _notice(rows, retired)


def run(current, interval=None):
    """Clear the screen and print a fresh frame every `interval` seconds until interrupted.

    A None interval takes the cadence from [dashboard] interval, since the dashboard runs
    in its own process and reads the settings itself.
    """
    interval = (
        config.number("dashboard", "interval")
        if interval is None
        else config.in_range(interval, "--interval")
    )
    try:
        while True:
            print("\033[2J\033[H" + render(current), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def dashboard_ratio():
    """Fraction of the captain pane kept when the dashboard splits off below it: the
    dashboard is a few rows of table, not half a screen."""
    return config.number("dashboard", "ratio", below=1)


def start_dashboard(current, pane, project):
    """Split a short pane below the captain and run `captain dashboard` in it."""
    # Env var, not shell text, so a quote in the session path can't inject commands.
    launcher = current.directory / "dashboard.sh"
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.join(
            [sys.executable, "-m", "captain_barbossa", "--session", current.meta["id"], "dashboard"]
        )
        + "\n",
        encoding="utf-8",
    )
    launcher.chmod(0o600)
    split_args = [
        "--pane",
        pane["pane_id"],
        "--direction",
        "down",
        "--ratio",
        str(dashboard_ratio()),
        "--cwd",
        str(project),
        "--no-focus",
        "--env",
        f"CAPTAIN_SESSION={current.meta['id']}",
        "--env",
        f"CAPTAIN_PROJECT={project.resolve()}",
        "--env",
        f"CAPTAIN_STATE_ROOT={state_root()}",
        "--env",
        f"CAPTAIN_TEMP_ROOT={temp_root()}",
        "--env",
        f"CAPTAIN_DASHBOARD_LAUNCHER={launcher}",
    ]
    created = runtime.herdr("pane", "split", *split_args)
    board = created.get("pane", {}).get("pane_id")
    if not isinstance(board, str) or not board:
        raise CaptainError("Herdr split a pane for the dashboard but returned no pane ID.")
    runtime.herdr("pane", "rename", board, "Dashboard")
    # A new shell may still be in canonical mode: keep terminal input short.
    runtime.herdr(
        "pane", "run", board, '/bin/sh "$CAPTAIN_DASHBOARD_LAUNCHER"', expect_output=False
    )
    return board


def dashboard_pane(current):
    """The dashboard's pane id from captain.json, or None when this session has none."""
    try:
        record = read_json(current.directory / "captain.json")
    except CaptainError:
        return None
    return record.get("dashboard") if isinstance(record, dict) else None


def renest_dashboard(pane, captain_pane):
    """Re-attach the dashboard directly under the captain pane after a crew split.

    Splitting the captain pane sideways turns it into a row, and the dashboard, its former
    sibling, ends up under that whole row instead of under the captain. `pane move` is the
    only Herdr command that reparents, and it is a no-op within one tab, so the dashboard
    goes out to a scratch tab and straight back. Herdr closes the emptied tab itself.
    """
    runtime.herdr("pane", "move", pane, "--new-tab", "--no-focus")
    runtime.herdr(
        "pane",
        "move",
        pane,
        "--tab",
        captain_pane["tab_id"],
        "--target-pane",
        captain_pane["pane_id"],
        "--split",
        "down",
        "--ratio",
        str(dashboard_ratio()),
        "--no-focus",
    )


def run_dashboard(args, pane, project):
    """Refresh the crew token-usage table in this pane until interrupted."""
    current = read_session(project, args.session, pane)
    if getattr(args, "refresh_prices", False):
        usage._prices(cached_only=False)
    # Omitted --interval leaves the cadence to dashboard.run, which reads the setting.
    run(current, args.interval)
