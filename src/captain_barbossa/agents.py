"""Launch native captain and crew sessions in Herdr."""

import json
import os
import re
import shlex
import sys
from itertools import cycle
from uuid import uuid4

from . import config, dashboard, instructions, protocol, runtime, usage
from .crew import Crew
from .memory import (
    add_memory,
    agent_name,
    crew_meta,
    private_dir,
    prune_sessions,
    read_cursor,
    read_events,
    read_json,
    read_session,
    session,
    state_root,
    temp_root,
    truncate_label,
    write_json,
)
from .models import HOOKLESS, PROVIDERS, model_names, resolve_model
from .pane import MODEL_TIMEOUT, PROMPT_TIMEOUT, shell_ready_for_input
from .pi_captain import captain_extension
from .placement import Placement
from .prompts import PLACEMENTS, choose
from .runtime import HERDR_ERRORS, CaptainError, check_text, executable
from .update_check import check_for_update

# A hook payload carries a whole assistant turn or tool_input; the wait line only needs
# enough to decide what to do next, and the event file keeps the rest.
HOOK_LIMIT = 500

# One word each: the roster name is the crew ID, the display name, and the agent suffix.
CREW_NAMES = (
    "jack",
    "will",
    "elizabeth",
    "gibbs",
    "anamaria",
    "pintel",
    "ragetti",
    "cotton",
    "marty",
    "tia",
    "davy",
    "sao",
)


def dashboard_ratio():
    """Fraction of the captain pane kept when the dashboard splits off below it: the
    dashboard is a few rows of table, not half a screen."""
    return config.number("dashboard", "ratio", below=1)


def launch(args, pane, project):
    if not sys.stdin.isatty():
        raise CaptainError("Launch captain from an interactive Herdr terminal.")
    check_for_update()
    wanted = args.agent or config.text("captain", "agent")
    if wanted and wanted not in PROVIDERS:
        where = config.source("captain", "agent") or config.SETTINGS_PATH
        raise CaptainError(
            f"[captain] agent is '{wanted}', which is not one of {', '.join(PROVIDERS)}. "
            f"Fix it in {where}."
        )
    provider = choose(wanted, PROVIDERS, "Choose your captain", "--agent")
    binary = executable(provider)
    current = session(project, pane, args.session, create=args.session is None)
    meta = current.meta
    try:
        prune_sessions(project, current=meta["id"])
    except HERDR_ERRORS:
        pass  # retention is housekeeping; never block a launch on it
    instruction_text = instructions.agent_instructions(
        current.directory, "Captain Barbossa", provider
    )
    add_memory(current.graph, f"session:{meta['id']}", "captain", provider)
    runtime.herdr("tab", "rename", pane["tab_id"], "Captain Barbossa")
    env = dict(
        os.environ,
        CAPTAIN_SESSION=meta["id"],
        CAPTAIN_PROJECT=str(project.resolve()),
        CAPTAIN_STATE_ROOT=str(state_root()),
        CAPTAIN_TEMP_ROOT=str(temp_root()),
    )
    # The captain's own hooks write the same lifecycle events crew do, so the dashboard
    # can find its transcript. Nothing reads this file as a roster entry.
    events = current.events("captain")
    private_dir(events.parent)
    wanted = config.text("captain", "model")
    model = resolve_model(provider, wanted) if wanted else None
    command = [binary, *instructions.native_args(provider, instruction_text, model, events=events)]
    if provider == "pi":
        command.extend(["--extension", str(captain_extension(current.directory))])
    if args.prompt:
        command.extend(["--", args.prompt])
    board = None
    if config.flag("dashboard", "enabled") and not args.no_dashboard:
        try:
            board = start_dashboard(current, pane, project)
        except Exception as exc:  # a dashboard pane must never cost the captain its launch
            print(f"captain: no dashboard pane: {exc}", file=sys.stderr)
    write_json(
        current.directory / "captain.json",
        {
            "provider": provider,
            "pane": pane["pane_id"],
            "terminal_id": pane.get("terminal_id"),
            "dashboard": board,
        },
    )
    os.execvpe(binary, command, env)


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
    dashboard.run(current, args.interval)


def tail_note(current, crew, tail):
    """The wait line's pane-tail fragment: inline, or a file path for pi.

    pi installs no hooks, so every pi wait falls back to the pane tail, and the captain
    extension steers whatever wait prints straight into the conversation. Filing it keeps
    each delivery one short line; the captain reads the path only when it needs detail.
    """
    if crew.record.get("provider") != "pi":
        return f"pane tail: {tail}"
    path = current.directory / f"tail-{crew.crew_id}.txt"
    path.write_text(tail, encoding="utf-8")
    path.chmod(0o600)
    return f"pane tail ({len(tail)} chars): {path}"


def wait_crew(args, pane, project):
    current, crew = Crew.for_args(args, pane, project)
    if crew.record.get("incarnation_id") or getattr(args, "json", False):
        timeout = (
            config.number("crew", "wait_timeout", kind=int)
            if args.timeout is None
            else config.in_range(args.timeout, "--timeout", least=0)
        )
        response = protocol.wait(crew, timeout, getattr(args, "ack", None))
        print(
            json.dumps(response)
            if args.json
            else f"{response['crew']} {response['status']}: {response['summary']} "
            f"(delivery {response['delivery_id']}; acknowledge with wait --ack)"
        )
        return
    if getattr(args, "ack", None):
        raise CaptainError("Legacy wait has no acknowledged deliveries.")
    report_cursor = crew.report_cursor
    # Reports are consumed by cursor alone: pi installs no hooks, so an events file may
    # never exist, and a report written before this wait started is still undelivered.
    reported = read_cursor(report_cursor)
    timeout = (
        config.number("crew", "wait_timeout", kind=int)
        if args.timeout is None
        # 0 is allowed on the flag alone: it means report whatever has already arrived.
        else config.in_range(args.timeout, "--timeout", least=0)
    )
    status, event = crew.status(timeout)
    if status is None:
        raise CaptainError(
            f"{crew.display_name} was still working after {timeout} seconds. "
            "Wait again, or read its pane with: herdr agent read " + crew.record["agent"]
        )
    all_reports = crew.reports
    reports = all_reports[reported:]
    if reports:
        entry = f"{status}; reported: {reports[-1]}"
        # Blocked means the pane is waiting on input/approval; show it even with a report.
        printed = (
            f"{entry}; {tail_note(current, crew, crew.pane.tail())}"
            if status == "blocked" and event is None
            else entry
        )
        # The report text already lives on its own "report" edge; don't duplicate it here.
        completed = f"{status}; reported"
    elif event is not None:
        entry = f"{status}; no report recorded"
        printed = entry
        completed = entry
    else:
        tail = crew.pane.tail()
        entry = f"{status}; no report recorded"
        printed = f"{entry}; {tail_note(current, crew, tail)}"
        completed = entry
    if event is not None and (not reports or status == "blocked"):
        message = (
            event.get("last-assistant-message")
            or event.get("last_assistant_message")
            or event.get("message")
        )
        if status == "blocked" and event.get("tool_name"):
            message = f"{event['tool_name']} {json.dumps(event.get('tool_input', {}))}"
        if message:
            capped = truncate_label(message, HOOK_LIMIT)
            if capped != message:
                capped += f" (full event: {crew.events})"
            printed += f"; hook: {capped}"
    add_memory(current.graph, crew.display_name, "completed", completed)
    # pi never creates the events directory this cursor lives beside.
    private_dir(report_cursor.parent)
    write_json(report_cursor, len(all_reports))
    print(f"{crew.display_name} {status}.")
    print(printed)


def focus_crew(args, pane, project):
    _, crew = Crew.for_args(args, pane, project)
    try:
        agent = runtime.herdr("agent", "get", crew.record["agent"]).get("agent", {})
        # The live agent wins over the recorded tab when its pane has moved.
        tab_id = agent.get("tab_id") or crew.record.get("tab")
        if tab_id and tab_id != pane["tab_id"]:
            runtime.herdr("tab", "focus", tab_id)
        runtime.herdr("agent", "focus", crew.record["agent"])
    except CaptainError as exc:
        raise CaptainError(f"Could not focus {crew.display_name}: {exc}") from exc
    print(f"Focused {crew.display_name}.")


def interrupt_crew(args, pane, project):
    """Stop a crew's current turn, keeping its pane, conversation and assignment.

    Mail is a data channel delivered at a turn boundary, which is right: a message is
    read when the crew next draws breath. "Stop what you are doing" is not that, and
    routing it through mail makes a lifecycle action into text the crew reasons about
    at its leisure. This is the separate channel, and the only thing that reaches a
    crew mid-turn.

    Escape interrupts without ending the session, so the crew keeps its context and its
    assignment stays open. Nothing is torn down and no work is discarded.
    """
    current, crew = Crew.for_args(args, pane, project)
    if crew.is_dismissed:
        raise CaptainError(f"{crew.display_name} was already dismissed.")
    try:
        runtime.herdr("agent", "send-keys", crew.record["agent"], "escape")
    except CaptainError as exc:
        raise CaptainError(f"Could not interrupt {crew.display_name}: {exc}") from exc
    add_memory(current.graph, crew.record["agent"], "interrupted", args.reason or "captain")
    # A busy crew held every ring, so mail queued during that turn is still waiting
    # behind the drain cooldown. It is idle now, so ring it instead of making the captain
    # wait out an interval for the instruction they interrupted in order to give.
    waiting = protocol.unread(crew)
    if waiting:
        protocol.ring(crew, waiting[0]["id"])
    print(
        f"Interrupted {crew.display_name}."
        + (f" Rang {len(waiting)} waiting message(s)." if waiting else "")
    )


def status_crew(args, pane, project):
    """Print a table of this session's crew, refreshing status from Herdr best-effort."""
    current = read_session(project, args.session, pane)
    rows = []
    path = current.directory / "protocol.json"
    state = read_json(path) if path.exists() else None
    for crew in Crew.members(current):
        if crew.is_dismissed and not args.all:
            continue
        status = crew.record.get("status") or "-"
        try:
            agent = runtime.herdr("agent", "get", crew.record["agent"], timeout=5).get("agent", {})
            status = agent.get("agent_status") or status
        except HERDR_ERRORS:
            pass
        task = (crew.record.get("task") or "").splitlines()
        if crew.record.get("incarnation_id") and state and not crew.is_dismissed:
            assignment = protocol.active(state, crew)
            task = assignment["original_task"].splitlines()
            status = "idle" if status == "done" else status
            if assignment["state"] in ("asked", "done"):
                status = assignment["state"]
            if protocol.unread(crew):
                try:
                    gate = crew.pane.nudge_block(crew)
                except HERDR_ERRORS:
                    gate = None
                if gate:
                    status = f"{status}; mail held: {gate}"
        rows.append(
            (
                crew.display_name,
                crew.record.get("provider", "-"),
                crew.record.get("model") or "-",
                crew.record.get("pane") or "-",
                status,
                truncate_label(task[0], 60) if task else "-",
            )
        )
    if not rows:
        print("No crew.")
        return
    headers = ("NAME", "PROVIDER", "MODEL", "PANE", "STATUS", "TASK")
    widths = [max(len(str(row[i])) for row in (headers, *rows)) for i in range(len(headers) - 1)]
    for row in (headers, *rows):
        cells = [str(value).ljust(width) for value, width in zip(row, widths)]
        cells.append(str(row[-1]))
        print("  ".join(cells))


def tell_crew(args, pane, project):
    """Send a follow-up prompt to a running crew."""
    check_text(args.message, "message")
    current = read_session(project, args.session, pane)
    crew = Crew.resolve(current, args.name)
    if crew.record.get("incarnation_id"):
        if crew.is_dismissed:
            raise CaptainError("Crew was dismissed; recruit new crew instead.")
        if not args.assignment:
            raise CaptainError("Protocol follow-ups require --assignment ID.")
        message_id = protocol.deliver(crew, args.message, assignment_id=args.assignment)
        print(json.dumps({"crew": crew.display_name, "message_id": message_id}))
        return
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        crew = Crew.resolve(current, args.name)
        if crew.is_dismissed:
            raise CaptainError(f"{crew.display_name} was dismissed; recruit new crew instead.")
        events = crew.events
        cursor = crew.cursor
        # An idle event from the gap before this prompt would otherwise read as done.
        _, offset = read_events(events, read_cursor(cursor))
        if events.exists():
            write_json(cursor, offset)
        crew.record["task"] = args.message
        crew.pane.submit_task(args.message, crew.record.get("provider"))
        add_memory(current.graph, crew.display_name, "assigned", args.message)
    print(f"Sent to {crew.display_name}.")


def protocol_command(args, pane, project):
    _, crew = Crew.for_args(args, pane, project)
    if crew.is_dismissed:
        raise CaptainError("Crew was dismissed; recruit new crew instead.")
    if args.command == "assign":
        assignment = protocol.begin(crew, project, args.task, args.owns, args.allow, args.handoff)
        message_id = protocol.deliver(crew, args.task, assignment_id=assignment["id"], initial=True)
        response = {"assignment_id": assignment["id"], "message_id": message_id}
    elif args.command == "answer":
        message_id = protocol.deliver(
            crew, args.message, assignment_id=args.assignment, question_id=args.question_id
        )
        response = {"assignment_id": args.assignment, "message_id": message_id}
    else:
        response = protocol.change(crew, args, project)
    print(json.dumps(response))


def switch_model(args, pane, project):
    """Switch a running crew to another model through the native CLI's own /model command."""
    current, crew = Crew.for_args(args, pane, project)
    provider = crew.record["provider"]
    model = resolve_model(provider, args.model)
    crew_agent = crew.record["agent"]
    names = [name.casefold() for name in model_names(provider, model)]
    if (
        crew.pane.agent_status() not in ("idle", "working", "done")
        or crew.pane.choice_modal()
        or crew.pane.draft_pending(provider)
    ):
        raise CaptainError(
            "Model switch needs a readable empty composer without an approval prompt."
        )
    if provider == "claude":
        runtime.herdr("agent", "prompt", crew_agent, f"/model {model}")
        if not crew.pane.model_landed(names, PROMPT_TIMEOUT, provider):
            # Claude Code can leave a submitted line as an unsent draft in its input box.
            if (
                crew.pane.agent_status() == "blocked"
                or crew.pane.choice_modal()
                or crew.pane.composer(provider) != f"/model {model}"
            ):
                raise CaptainError(
                    "Model switch delivery is unknown; inspect the pane before retrying."
                )
            runtime.herdr("agent", "send-keys", crew_agent, "enter")
    elif provider in HOOKLESS:
        # An exact model ID selects straight away, with no picker and no draft state.
        runtime.herdr("agent", "prompt", crew_agent, f"/model {model}")
    else:
        runtime.herdr("agent", "prompt", crew_agent, "/model")
        crew.pane.codex_pick_model(model, MODEL_TIMEOUT)
    if not crew.pane.model_landed(names, MODEL_TIMEOUT, provider):
        raise CaptainError(
            f"{crew.display_name} did not confirm the switch to {model}. "
            f"Read its pane before retrying: herdr agent read {crew_agent}"
        )
    with crew_meta(current.directory) as meta:
        meta["crew"][crew.crew_id]["model"] = model
    add_memory(current.graph, crew.display_name, "model", model)
    print(f"{crew.display_name} switched to {model}.")
    if provider == "claude":
        # Claude Code's inline /model always writes the model to the user's settings.
        print("Claude Code also saved it as the default for new sessions.")


def never_read(assignment):
    """Whether any message ever reached the crew. An enqueue is not a delivery."""
    return not any(message["delivery"] == "read" for message in assignment["messages"])


def dismiss_blocker(crew, assignment):
    """Name the one thing that actually holds the dismissal, not both at once."""
    if never_read(assignment):
        # A launch that failed before its first delivery, or mail the crew never read:
        # nothing reached it, so there is nothing to report and no crew process alive to
        # run done. Refusing here would reserve the name forever.
        return
    if assignment["state"] != "done":
        raise CaptainError(
            f"{crew.display_name} has no done report; finish with a report before dismissal."
        )
    unacked = len(assignment["notices"]) - assignment["ack_seq"]
    if assignment["pending"]:
        # A notification returned but never acked; a native one has no notice behind it to count.
        unacked = max(unacked, 1)
    if unacked:
        raise CaptainError(
            f"{crew.display_name} has {unacked} unacknowledged notification(s); "
            "acknowledge them with wait --ack before dismissal."
        )


def dismiss_crew(args, pane, project):
    current = session(project, pane, args.session)
    bounced, undelivered = [], False
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        crew = Crew.resolve(current, args.name)
        if crew.is_dismissed:
            raise CaptainError(f"{crew.display_name} was already dismissed.")
        if crew.record.get("incarnation_id"):
            with protocol.checkpoint(current.directory) as state:
                assignment = protocol.active(state, crew)
                # Same question the blocker asks: an assignment nothing reached must not keep
                # its paths reserved by lingering in "working" after the crew is gone.
                undelivered = never_read(assignment)
                dismiss_blocker(crew, assignment)
            # protocol.bounce takes protocol.lock itself; call it outside the checkpoint
            # above, or a crew with mail still queued at dismissal would deadlock here.
            bounced = protocol.unread(crew)
            for message in bounced:
                protocol.bounce(crew, message["id"], "Crew dismissed before mail was read.")
        if not crew.record.get("pane") and not crew.record.get("incarnation_id"):
            raise CaptainError(f"{crew.display_name} has no recorded pane to close.")
        try:
            if crew.record.get("pane"):
                runtime.herdr("pane", "close", crew.record["pane"])
        except CaptainError as exc:
            if not re.search(r"\bpane_not_found\b", str(exc)):
                raise CaptainError(f"Could not dismiss {crew.display_name}: {exc}") from exc
        if undelivered:
            # Close the reservation with its reason; a working assignment would linger forever.
            with protocol.checkpoint(current.directory) as state:
                assignment = protocol.active(state, crew)
                assignment["state"] = "done"
                assignment["report"] = "Dismissed before any message was read."
        (current.directory / f"crew-{crew.crew_id}.sh").unlink(missing_ok=True)
        crew.record["status"] = "dismissed"
        tab_id = crew.record.get("tab")
        label = Crew.tab_label(current, tab_id) if tab_id and tab_id != pane["tab_id"] else None
    # Rename after the roster write: a stale tab id must not cost the dismissal record.
    if label:
        runtime.herdr("tab", "rename", tab_id, label)
    add_memory(current.graph, f"session:{meta['id']}", "dismissed", crew.record["agent"])
    for message in bounced:
        print(f"Bounced unread mail to {crew.display_name}: {message['id']}.")
    print(f"Dismissed {crew.display_name}.")


def create_crew(args, pane, project):
    if args.name is not None and (
        len(args.name) > 16
        or not re.fullmatch(rf"({'|'.join(CREW_NAMES)})(-[1-9][0-9]*)?", args.name)
    ):
        raise CaptainError(
            "Use a Pirates of the Caribbean character name (e.g. jack or gibbs), "
            "or omit NAME to assign one automatically. Names must be at most 16 characters."
        )
    check_text(args.task, "task")
    provider = choose(args.crew_agent, PROVIDERS, "Choose your crew agent", "--agent")
    placement = choose(args.placement, PLACEMENTS, "Where should the crew open?", "--placement")
    current = session(project, pane, args.session)
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        board = dashboard_pane(current)
        spot = Placement(pane, current, board).choose_split(args, placement)
        direction, split_pane, tab_id, auto = spot.direction, spot.pane, spot.tab, spot.reason
        if auto and split_pane is None:
            placement = "tab"
        wanted = args.model or config.text("crew", "model")
        model = resolve_model(provider, wanted)
        print(f"Model: {model} (from {wanted!r})", file=sys.stderr)
        binary = executable(provider)
        name = args.name
        if name is None:
            for index, character in enumerate(cycle(CREW_NAMES)):
                round_number = index // len(CREW_NAMES) + 1
                name = f"{character}-{round_number - 1}" if round_number > 1 else character
                if not Crew.name_reserved(current, name):
                    break
        display_name = name.capitalize()
        if Crew.name_reserved(current, name):
            raise CaptainError(f"Crew '{display_name}' already exists in this session.")
        crew_agent = agent_name(meta["id"], name)
        incarnation = uuid4().hex
        crew = Crew(
            name,
            {"id": name, "name": display_name, "agent": crew_agent, "incarnation_id": incarnation},
            current,
        )
        assignment = protocol.begin(crew, project, args.task, args.owns, args.allow, args.handoff)
        # A failed layout/launcher must leave a resolvable owner for the reservation.
        crew.record.update(
            assignment_id=assignment["id"],
            provider=provider,
            task=args.task,
            status="needs_attention",
        )
        meta["crew"][name] = crew.record
        write_json(current.meta_path, meta)
        launcher = current.directory / f"crew-{name}.sh"
        environment = [
            "--env",
            "CAPTAIN_ROLE=crew",
            "--env",
            f"CAPTAIN_CREW={name}",
            "--env",
            f"CAPTAIN_INCARNATION={incarnation}",
            "--env",
            f"CAPTAIN_ASSIGNMENT={assignment['id']}",
            "--env",
            f"CAPTAIN_SESSION={meta['id']}",
            "--env",
            f"CAPTAIN_PROJECT={project.resolve()}",
            "--env",
            f"CAPTAIN_STATE_ROOT={state_root()}",
            "--env",
            f"CAPTAIN_TEMP_ROOT={temp_root()}",
            "--env",
            f"CAPTAIN_CREW_LAUNCHER={launcher}",
        ]
        instruction_text = instructions.agent_instructions(
            current.directory, f"crew member {display_name}", provider
        )
        events = crew.events
        private_dir(events.parent)
        events.write_text("", encoding="utf-8")
        events.chmod(0o600)
        crew.cursor.unlink(missing_ok=True)
        write_json(crew.report_cursor, len(crew.reports))
        command = shlex.join(
            [binary, *instructions.native_args(provider, instruction_text, model, events)]
        )
        launcher.write_text(
            f"#!/bin/sh\nunset CAPTAIN_CREW_LAUNCHER\nexec {command}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o600)
        if placement == "tab":
            created = runtime.herdr(
                "tab",
                "create",
                "--workspace",
                pane["workspace_id"],
                "--cwd",
                str(project),
                "--label",
                display_name,
                "--no-focus",
                *environment,
            )
            new_pane = created.get("root_pane", {}).get("pane_id")
            tab_id = created.get("tab_id") or created.get("root_pane", {}).get("tab_id")
        else:
            try:
                created = runtime.herdr(
                    "pane",
                    "split",
                    "--pane",
                    split_pane,
                    "--direction",
                    "right" if direction == "vertical" else "down",
                    *(["--ratio", f"{spot.ratio:.4f}"] if spot.ratio else []),
                    "--cwd",
                    str(project),
                    "--no-focus",
                    *environment,
                )
            except CaptainError as exc:
                raise CaptainError(
                    f"Could not split pane {split_pane}; it may have closed: {exc}. "
                    "Ask the user which pane to split again."
                ) from exc
            new_pane = created.get("pane", {}).get("pane_id")
            tab_id = created.get("pane", {}).get("tab_id") or tab_id
            if board and tab_id == pane["tab_id"]:
                try:
                    renest_dashboard(board, pane)
                except HERDR_ERRORS as exc:  # geometry is cosmetic; never lose the crew over it
                    print(f"captain: dashboard not re-nested: {exc}", file=sys.stderr)
        if not new_pane:
            raise CaptainError(
                "Herdr created a layout but returned no pane ID. Inspect the workspace before retrying."
            )
        record = {
            "id": name,
            "incarnation_id": incarnation,
            "assignment_id": assignment["id"],
            "name": display_name,
            "agent": crew_agent,
            "provider": provider,
            "pane": new_pane,
            "tab": tab_id,
            "placement": placement,
            "direction": direction,
            "split_pane": split_pane,
            "auto": auto,
            # The slot this crew holds in its tab's declared shape. Absent for a crew
            # placed by hand, which the grid then counts nowhere and never splits.
            "column": spot.column,
            "row": spot.row,
            "model": model,
            "task": args.task,
            "status": "starting",
        }
        crew.record = record
        meta["crew"][name] = record
        write_json(current.meta_path, meta)
        try:
            add_memory(current.graph, f"session:{meta['id']}", "crew", crew_agent)
            add_memory(current.graph, crew_agent, "name", display_name)
            add_memory(current.graph, crew_agent, "assigned", args.task)
            if model:
                add_memory(current.graph, crew_agent, "model", model)
            if auto:
                add_memory(current.graph, crew_agent, "placement", f"auto: {auto}")
            runtime.herdr("pane", "rename", new_pane, display_name)
            if not shell_ready_for_input(new_pane):
                raise CaptainError(
                    f"Shell in pane {new_pane} is not ready for input; answer any interactive "
                    "question before retrying."
                )
            runtime.herdr(
                "pane", "run", new_pane, '/bin/sh "$CAPTAIN_CREW_LAUNCHER"', expect_output=False
            )
            crew.pane.wait_for_crew(new_pane, provider)
            protocol.deliver(crew, args.task, assignment_id=assignment["id"], initial=True)
            record["status"] = "started"
            if tab_id != pane["tab_id"]:
                label = Crew.tab_label(current, tab_id)
                if label:
                    runtime.herdr("tab", "rename", tab_id, label)
        except HERDR_ERRORS as exc:
            record["status"] = "needs_attention"
            write_json(current.meta_path, meta)
            raise CaptainError(
                f"Crew pane {new_pane} was created but startup needs attention: {exc}. "
                f"Inspect pane {new_pane} in Herdr. The pane was preserved, and launcher "
                f"{launcher} was preserved. If the pane is at a shell prompt, retry there with: "
                f"/bin/sh {shlex.quote(str(launcher))}. Once Herdr detects {provider} in that pane, "
                f"register it from the captain pane with: herdr agent rename {new_pane} {crew_agent}. "
                f"Verify with: herdr agent get {crew_agent}. Inspect assignment {assignment['id']} "
                "before using tell to deliver any unsent task; "
                "the task was not automatically retried."
            ) from exc
    print(json.dumps({key: value for key, value in record.items() if key != "task"}))
