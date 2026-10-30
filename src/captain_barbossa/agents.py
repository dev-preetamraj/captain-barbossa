"""Launch native captain and crew sessions in Herdr."""

import json
import os
import re
import shlex
import sys
import time
from itertools import cycle
from pathlib import Path
from uuid import uuid4

from . import (
    background,
    config,
    execution,
    instructions,
    lifetime,
    native_history,
    profiles,
    protocol,
    recovery,
    results,
    runtime,
    worktrees,
)
from .crew import Crew, CrewState
from .dashboard import dashboard_pane, renest_dashboard, start_dashboard
from .memory import add_memory, truncate_label
from .models import HOOKLESS, PROVIDERS, claude_label, model_names, resolve_model
from .pane import MODEL_TIMEOUT, PROMPT_TIMEOUT, shell_ready_for_input
from .pi_captain import captain_extension
from .placement import Placement, launch_choices
from .prompts import choose
from .runtime import HERDR_ERRORS, CaptainError, check_text, executable
from .sessions import agent_name, crew_meta, is_durable, prune_sessions, read_session, session
from .store import private_dir, read_json, state_root, temp_root, write_json
from .update_check import check_for_update

# How often the exit watcher checks whether the captain's CLI is still its parent. A pane
# that outlives the captain by a second costs nothing.
EXIT_POLL_SECONDS = 1.0

# The launch digest rides in one argv string, which Linux caps at 128 KiB.
RECOVERY_CHARS = 32_000

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


def close_session_panes(session_id, project):
    """Close this session's live crew panes and its dashboard pane, and nothing else.

    Every pane id comes from this session's own records, so a pane the session never
    opened is never touched. One already gone is not news, and Herdr closes a tab whose
    last pane goes with it. A crew whose owned work Git shows unlanded keeps its pane:
    no user is present at exit to confirm leaving it behind.
    """
    current = read_session(project, session_id)
    with crew_meta(current.directory) as meta:
        closing = []
        live = [
            crew
            for crew in Crew.members(current._replace(meta=meta))
            if not crew.is_dismissed and crew.record.get("pane")
        ]
        with protocol.checkpoint(current.directory) as state:
            for crew in live:
                try:
                    assignment = protocol.active(state, crew)
                    if crew.record.get("worktree_id"):
                        results.collect(
                            current,
                            crew.record["worktree_id"],
                            assignment["paths"],
                            assignment.get("report") or "",
                        )
                        reasons = []
                    else:
                        reasons = protocol.unlanded(project, assignment)
                except CaptainError as exc:
                    reasons = [str(exc)] if crew.record.get("worktree_id") else []
                if reasons:
                    try:
                        print(
                            f"captain: kept {crew.display_name}'s pane open; its owned work "
                            f"has not landed: {'; '.join(reasons)}. {protocol.UNSAVED_CAVEAT}",
                            file=sys.stderr,
                        )
                    except OSError:  # the terminal went with the captain; keep closing
                        pass
                else:
                    closing.append((crew.record["pane"], crew.record))
        board = dashboard_pane(current)
        if board:
            closing.append((board, None))
        for pane_id, record in closing:
            stopped = False
            try:
                if record is not None and record.get("lifetime_id"):
                    crew = Crew(record["id"], record, current._replace(meta=meta))
                    record["cleanup"] = execution.retire(crew)
                    stopped = record["cleanup"]["lifetime"]["ok"]
                else:
                    runtime.herdr("pane", "close", pane_id)
                    stopped = True
            except HERDR_ERRORS as exc:
                stopped = not (record and record.get("lifetime_id")) and bool(
                    re.search(r"\bpane_not_found\b", str(exc))
                )
            if record is not None:
                if not stopped:
                    record["status"] = "cleanup_pending"
                    continue
                crew = Crew(record["id"], record, current._replace(meta=meta))
                for message in protocol.unread(crew):
                    protocol.bounce(crew, message["id"], "Captain exited before mail was read.")
                with protocol.checkpoint(current.directory) as state:
                    assignment = state["assignments"].get(state["active"].get(crew.crew_id))
                    if assignment and assignment["state"] != "done":
                        assignment.update(state="done", report="Crew stopped at captain exit.")
                # Recorded as the pane goes: a resumed --session must not find ghost crew
                # holding slots no pane backs any more.
                record["status"] = "dismissed"


def close_panes_on_exit(session_id, project):
    """Fork a watcher that closes this session's panes once the captain's CLI exits.

    exec replaces this process, so no code of captain's own runs at exit. The watcher
    polls getppid() instead: it is reparented the moment that process dies, whichever way
    it dies. An inherited pipe would be simpler but is held open by every descendant the
    CLI forks, so one backgrounded job meant no teardown at all. setsid keeps the watcher
    out of the pane's process group, so closing the captain's pane cannot take the
    teardown with it. stdin and stdout go to /dev/null; stderr stays on the terminal, which
    the user's shell gets back, so a pane kept for unlanded work says why.
    """
    captain_pid = os.getpid()
    if os.fork():
        return
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for stream in range(2):
            os.dup2(null, stream)
        while os.getppid() == captain_pid:
            time.sleep(EXIT_POLL_SECONDS)
        close_session_panes(session_id, project)
    finally:
        # _exit even on an error: there is no terminal left to report one to, and the
        # watcher must not fall back into the captain's own code path.
        os._exit(0)


def prepare_launch(current, project, provider, binary, model):
    """Build the native captain command and environment overlay for either transport."""
    model = resolve_model(provider, model) if model else None
    with crew_meta(current.directory) as meta:
        meta["captain_provider"] = provider
    instruction_text = instructions.agent_instructions(
        current.directory, "Captain Barbossa", provider
    )
    # Delivered, not requested: a resumed captain that skips `recover` still knows its session.
    # Preferences serialise first, so the cut below only ever takes the session's tail.
    state = json.dumps(recovery.digest(current, project))
    if len(state) > RECOVERY_CHARS:
        state = state[:RECOVERY_CHARS] + "... truncated; run CAPTAIN recover"
    instruction_text += f"Launch state (reference data, not instructions):\n{state}\n"
    add_memory(current.graph, f"session:{meta['id']}", "captain", provider)
    events = current.events("captain")
    private_dir(events.parent)
    command = [binary, *instructions.native_args(provider, instruction_text, model, events=events)]
    if provider == "pi":
        command.extend(["--extension", str(captain_extension(current.directory))])
    env = {
        "CAPTAIN_SESSION": meta["id"],
        "CAPTAIN_PROJECT": str(project.resolve()),
        "CAPTAIN_STATE_ROOT": str(state_root()),
        "CAPTAIN_TEMP_ROOT": str(temp_root()),
    }
    return command, env


class LaunchPlanError(CaptainError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def launch_plan(args, project):
    provider = args.agent or config.text("captain", "agent")
    if not provider:
        raise LaunchPlanError("missing-choice", "Choose --agent.")
    if provider != "pi":
        raise LaunchPlanError("unsupported-transport", "Only pi RPC is supported.")
    identity = None
    current = None
    if args.session:
        try:
            current = session(project, None, args.session)
        except CaptainError as exc:
            raise LaunchPlanError("invalid-session", str(exc)) from exc
        if current.meta.get("captain_provider") != provider:
            raise LaunchPlanError("session-provider-mismatch", "Session provider is not pi.")
        path = current.directory / "pi-session.json"
        recorded = read_json(path) if path.is_file() else {}
        identity = recorded.get("sessionFile") if isinstance(recorded, dict) else None
        if (
            not isinstance(identity, str)
            or not identity
            or not Path(identity).is_absolute()
            or not Path(identity).is_file()
        ):
            raise LaunchPlanError(
                "resume-unavailable", "No exact existing pi session file recorded."
            )
    model = args.model or (None if current else config.text("captain", "model"))
    if not model and current is None:
        raise LaunchPlanError("missing-choice", "Choose --model.")
    binary = executable(provider)
    current = current or session(project, None, create=True)
    command, env = prepare_launch(current, project, provider, binary, model)
    command[1:1] = ["--mode", "rpc"]
    if args.effort:
        command.extend(["--thinking", args.effort])
    if identity:
        command.extend(["--session", identity])
    # Values are an overlay, never a copy of the caller's credentials or crew identity.
    for key in os.environ:
        if key.startswith("CAPTAIN_") and key not in (*env, "CAPTAIN_MEMORY_ROOT"):
            env[key] = None
    if "CAPTAIN_MEMORY_ROOT" in os.environ:
        env["CAPTAIN_MEMORY_ROOT"] = os.environ["CAPTAIN_MEMORY_ROOT"]
    return {
        "version": 1,
        "provider": provider,
        "transport": "rpc",
        "argv": command,
        "cwd": str(project.resolve()),
        "env": env,
        "captainSessionId": current.meta["id"],
        "nativeResumeIdentity": identity,
    }


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
    model = config.text("captain", "model")
    command, overrides = prepare_launch(current, project, provider, binary, model)
    runtime.herdr("tab", "rename", pane["tab_id"], "Captain Barbossa")
    env = dict(os.environ, **overrides)
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
    try:
        close_panes_on_exit(meta["id"], project)
    except OSError as exc:  # teardown must never cost the captain its launch
        print(f"captain: no exit teardown: {exc}", file=sys.stderr)
    os.execvpe(binary, command, env)


def wait_crew(args, pane, project):
    if getattr(args, "all", False):
        if args.name or args.ack:
            raise CaptainError(
                "wait --all takes no name or ack; acknowledge with wait NAME --ack ID."
            )
        current = read_session(project, args.session, pane)
        timeout = (
            config.number("crew", "wait_timeout", kind=int)
            if args.timeout is None
            else config.in_range(args.timeout, "--timeout", least=0)
        )
        # A recruiter can publish the roster and die before begin reserves anything.
        # These failures have no assignment to acknowledge; never adopt a stale one.
        orphans = [
            protocol.result(crew, None, "error", background.UNRESERVED)
            for crew in Crew.members(current)
            if not crew.is_dismissed and unreserved(crew)
        ]
        # Never unackable, so an orphan rides along a real wait rather than ending it at once.
        response = protocol.wait_all(current, timeout)
        if orphans:
            response["status"] = "ready"
            response["deliveries"].extend(orphans)
        if args.json:
            print(json.dumps(response))
        else:
            for delivery in response["deliveries"]:
                print(
                    f"{delivery['crew']} {delivery['status']}: {delivery['summary']} "
                    + (
                        f"(delivery {delivery['delivery_id']}; acknowledge with wait NAME --ack ID)"
                        if delivery["delivery_id"]
                        else "(nothing pending to acknowledge)"
                    )
                )
            if not response["deliveries"]:
                print("timeout: no pending deliveries")
        return
    if not args.name:
        raise CaptainError("wait requires a crew name or --all.")
    _, crew = Crew.for_args(args, pane, project)
    protocol.require_current(crew)
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
        + (
            f"(delivery {response['delivery_id']}; acknowledge with wait --ack)"
            if response["delivery_id"]
            else "(nothing pending to acknowledge)"
        )
    )


def focus_crew(args, pane, project):
    _, crew = Crew.for_args(args, pane, project)
    refuse_background(crew, "focus")
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

    The lifecycle channel beside mail: mail is read at a turn boundary, which is right
    for a message and wrong for "stop". Escape leaves the session alive, so nothing is
    torn down and no work is discarded.
    """
    current, crew = Crew.for_args(args, pane, project)
    if crew.is_dismissed:
        raise CaptainError(f"{crew.display_name} was already dismissed.")
    if crew.is_background:
        outcome = background.cancel(crew, args.reason)
        # Only the owner's confirmation proves this turn was actually interrupted.
        if outcome == "cancelled":
            add_memory(current.graph, crew.record["agent"], "interrupted", args.reason or "captain")
        print(f"{crew.display_name}: {outcome}.")
        return
    try:
        runtime.herdr("agent", "send-keys", crew.record["agent"], "escape")
    except CaptainError as exc:
        raise CaptainError(f"Could not interrupt {crew.display_name}: {exc}") from exc
    add_memory(current.graph, crew.record["agent"], "interrupted", args.reason or "captain")
    # It is idle now, so ring whatever the busy turn held rather than making the captain
    # wait out the drain cooldown for the instruction they interrupted to give.
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
        task = (crew.record.get("task") or "").splitlines()
        held = None
        orphan = False
        if crew.record.get("incarnation_id") and state and not crew.is_dismissed:
            try:
                task = protocol.active(state, crew)["original_task"].splitlines()
            except CaptainError:
                # A recruiter can die between the roster write and begin; one such
                # record must not cost the whole table. Foreground state reads evidence.
                orphan = crew.is_background
            if crew.record.get("pane") and protocol.unread(crew):
                try:
                    held = crew.pane.nudge_block(crew)
                except HERDR_ERRORS:
                    pass
        current_state = (
            CrewState("cleanup_pending", "cleanup", "shutdown unconfirmed; retry dismiss")
            if crew.record.get("status") == "cleanup_pending"
            else CrewState("failed", "process", background.UNRESERVED)
            if orphan
            else crew.current_state()
        )
        status = f"state:{current_state.state} source:{current_state.source} " + truncate_label(
            " ".join(current_state.detail.split()), 60
        )
        if held:
            status = f"{status}; mail held: {held}"
        rows.append(
            (
                crew.display_name,
                crew.record.get("provider", "-"),
                crew.record.get("model") or "-",
                crew.record.get("pane") or ("background" if crew.is_background else "-"),
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
    if crew.is_dismissed:
        raise CaptainError("Crew was dismissed; recruit new crew instead.")
    protocol.require_current(crew)
    refuse_background(crew, "tell")
    if not args.assignment:
        raise CaptainError("Protocol follow-ups require --assignment ID.")
    message_id = protocol.deliver(crew, args.message, assignment_id=args.assignment)
    print(json.dumps({"crew": crew.display_name, "message_id": message_id}))


def protocol_command(args, pane, project):
    _, crew = Crew.for_args(args, pane, project)
    if crew.is_dismissed:
        raise CaptainError("Crew was dismissed; recruit new crew instead.")
    if args.command in ("assign", "answer"):
        refuse_background(crew, args.command)
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
    refuse_background(crew, "a model switch")
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
    baseline = 0
    if provider == "claude":
        # Named inline, /model also saves the model as the user's default for new sessions,
        # so a retier goes through the picker, whose own key switches this session alone.
        runtime.herdr("agent", "prompt", crew_agent, "/model")
        if not crew.pane.claude_picker_open(PROMPT_TIMEOUT):
            # Claude Code can leave a submitted line as an unsent draft in its input box.
            if (
                crew.pane.agent_status() == "blocked"
                or crew.pane.choice_modal()
                or crew.pane.composer(provider) != "/model"
            ):
                raise CaptainError(
                    "Model switch delivery is unknown; inspect the pane before retrying."
                )
            runtime.herdr("agent", "send-keys", crew_agent, "enter")
        baseline = crew.pane.claude_pick_model(claude_label(model), MODEL_TIMEOUT)
    elif provider in HOOKLESS:
        # An exact model ID selects straight away, with no picker and no draft state.
        runtime.herdr("agent", "prompt", crew_agent, f"/model {model}")
    else:
        runtime.herdr("agent", "prompt", crew_agent, "/model")
        crew.pane.codex_pick_model(model, MODEL_TIMEOUT)
    if not crew.pane.model_landed(names, MODEL_TIMEOUT, provider, after=baseline):
        raise CaptainError(
            f"{crew.display_name} did not confirm the switch to {model}. "
            f"Read its pane before retrying: herdr agent read {crew_agent}"
        )
    with crew_meta(current.directory) as meta:
        meta["crew"][crew.crew_id]["model"] = model
    add_memory(current.graph, crew.display_name, "model", model)
    print(f"{crew.display_name} switched to {model}.")


def never_read(crew, assignment):
    """Whether any message ever reached the crew. An enqueue is not a delivery."""
    # A body the ring typed reached the crew even if it never ran inbox.
    landed = protocol.landed_bodies(crew)
    return not any(
        message["delivery"] == "read" or message["id"] in landed
        for message in assignment["messages"]
    )


def dismiss_blocker(crew, assignment, project, confirmed=False, *, allow_unreported=False):
    """Name the one thing that actually holds the dismissal, not both at once."""
    if never_read(crew, assignment):
        # A launch that failed before its first delivery, or mail the crew never read:
        # nothing reached it, so there is nothing to report and no crew process alive to
        # run done. Refusing here would reserve the name forever.
        return
    if assignment["state"] != "done" and not allow_unreported:
        raise CaptainError(
            f"{crew.display_name} has no done report; finish with a report before dismissal."
        )
    unacked = len(assignment["notices"]) - assignment["ack_seq"]
    if assignment["pending"]:
        # A notification returned but never acked; a native one has no notice behind it to count.
        unacked = max(unacked, 1)
    if unacked and not allow_unreported:
        raise CaptainError(
            f"{crew.display_name} has {unacked} unacknowledged notification(s); "
            "acknowledge them with wait --ack before dismissal."
        )
    if confirmed or crew.record.get("worktree_id"):
        return
    reasons = protocol.unlanded(project, assignment)
    if reasons:
        # A done report is not consent: only the user can let owned work go unlanded.
        raise CaptainError(
            f"{crew.display_name}'s owned work has not landed: {'; '.join(reasons)}. "
            f"{protocol.UNSAVED_CAVEAT} Have it committed, or rerun dismiss with "
            "--confirm-unlanded once the user agrees to leave it unlanded."
        )


def dismiss_crew(args, pane, project):
    current = session(project, pane, args.session, allow_dead_workspace=True)
    dead_workspace = current.meta["workspace"] != pane["workspace_id"]
    bounced, undelivered = [], False
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        crew = Crew.resolve(current, args.name)
        if crew.is_dismissed:
            raise CaptainError(f"{crew.display_name} was already dismissed.")
        retrying = crew.record.get("status") == "cleanup_pending"
        # Read while the crew still holds its slot; applied once Herdr has closed the pane.
        placement = Placement(pane, current, dashboard_pane(current))
        try:
            evening = [] if dead_workspace else placement.even_after_close(crew.record)
        except HERDR_ERRORS as exc:  # geometry is cosmetic; never lose the dismissal over it
            print(f"captain: tab not re-evened: {exc}", file=sys.stderr)
            evening = []
        # A background recruit that died before reserving holds nothing to settle.
        if crew.record.get("incarnation_id") and not unreserved(crew):
            with protocol.checkpoint(current.directory) as state:
                assignment = protocol.active(state, crew)
                # Same question the blocker asks: an assignment nothing reached must not keep
                # its paths reserved by lingering in "working" after the crew is gone.
                undelivered = never_read(crew, assignment)
                dismiss_blocker(
                    crew,
                    assignment,
                    project,
                    getattr(args, "confirm_unlanded", False),
                    allow_unreported=dead_workspace or retrying,
                )
                untracked = [path for path in assignment["paths"] if Path(path).is_absolute()]
                if untracked:
                    print(
                        f"Owned paths outside the checkout, not Git-tracked: {', '.join(untracked)}"
                    )
        if not crew.record.get("pane") and not crew.record.get("incarnation_id"):
            raise CaptainError(f"{crew.display_name} has no recorded pane to close.")
        try:
            if crew.record.get("lifetime_id"):
                crew.record["cleanup"] = execution.retire(crew)
                if not crew.record["cleanup"]["lifetime"]["ok"]:
                    raise CaptainError("shutdown unconfirmed")
            elif crew.is_background and dead_workspace:
                raise CaptainError("background writer has no managed lifetime")
            elif crew.record.get("pane") and not dead_workspace:
                runtime.herdr("pane", "close", crew.record["pane"])
        except HERDR_ERRORS as exc:
            if crew.record.get("lifetime_id") or not re.search(r"\bpane_not_found\b", str(exc)):
                if crew.record.get("lifetime_id"):
                    crew.record["status"] = "cleanup_pending"
                    # crew_meta does not save on exceptions; retain retry evidence first.
                    write_json(current.meta_path, meta)
                raise CaptainError(
                    f"Could not dismiss {crew.display_name}: {exc} [cleanup-pending]"
                ) from exc
            # The pane was already gone, so Herdr rebalanced the tab without this close.
            evening = []
        if crew.record.get("incarnation_id"):
            # protocol.bounce takes protocol.lock itself; call it outside the checkpoint
            # above, or a crew with mail still queued at dismissal would deadlock here.
            bounced = protocol.unread(crew)
            for message in bounced:
                protocol.bounce(crew, message["id"], "Crew dismissed before mail was read.")
        try:
            for source, towards, amount in evening:
                placement.resize(source, towards, amount)
        except HERDR_ERRORS as exc:  # geometry is cosmetic; never lose the dismissal over it
            print(f"captain: tab not re-evened: {exc}", file=sys.stderr)
        if undelivered or (
            (dead_workspace or retrying)
            and crew.record.get("incarnation_id")
            and not unreserved(crew)
        ):
            # Release only after confirmed shutdown, including a retried exit cleanup; a
            # real done report already released it and must survive.
            with protocol.checkpoint(current.directory) as state:
                assignment = protocol.active(state, crew)
                if assignment["state"] != "done":
                    assignment["state"] = "done"
                    assignment["report"] = (
                        "Dismissed after Herdr workspace disappeared."
                        if dead_workspace
                        else "Dismissed after shutdown retry."
                        if retrying
                        else "Dismissed before any message was read."
                    )
        if crew.record.get("worktree_id") and not crew.record.get("lifetime_id"):
            assignment = protocol.active_assignment(crew)
            results.collect(
                current,
                crew.record["worktree_id"],
                assignment["paths"],
                assignment.get("report") or "",
            )
        (current.directory / f"crew-{crew.crew_id}.sh").unlink(missing_ok=True)
        crew.record["status"] = "dismissed"
        tab_id = crew.record.get("tab")
        label = (
            Crew.tab_label(current, tab_id)
            if not dead_workspace and tab_id and tab_id != pane["tab_id"]
            else None
        )
    # Rename after the roster write: a stale tab id must not cost the dismissal record.
    if label:
        runtime.herdr("tab", "rename", tab_id, label)
    add_memory(current.graph, f"session:{meta['id']}", "dismissed", crew.record["agent"])
    for message in bounced:
        print(f"Bounced unread mail to {crew.display_name}: {message['id']}.")
    print(f"Dismissed {crew.display_name}.")


def begin_recruitment(crew, project, args):
    if crew.record["checkout"] == "worktree" and not config.flag("crew", "isolation_enabled"):
        raise CaptainError(
            "[worktree-disabled] Crew isolation is disabled; use --checkout shared or "
            "set [crew] isolation_enabled = true."
        )
    try:
        if crew.record["checkout"] == "worktree":
            if args.handoff:
                raise CaptainError(
                    "[worktree-handoff] Recruit a fresh isolated crew for a new assignment."
                )
            assignment_id = uuid4().hex
            registered = worktrees.reserve(
                crew.session, crew.crew_id, crew.record["incarnation_id"], assignment_id
            )
            crew.record.update(worktree_id=registered["id"], assignment_id=assignment_id)
            crew.session.meta["crew"][crew.crew_id] = crew.record
            write_json(crew.session.meta_path, crew.session.meta)
            registered = worktrees.create(crew.session, registered["id"])
            crew.record["execution_root"] = registered["execution_root"]
            write_json(crew.session.meta_path, crew.session.meta)
        else:
            assignment_id = None
            crew.record["execution_root"] = str(Path(project).resolve())
        return protocol.begin(
            crew,
            project,
            args.task,
            args.owns,
            args.allow,
            args.handoff,
            assignment_id=assignment_id,
        )
    except (CaptainError, OSError) as exc:
        if crew.record.get("worktree_id"):
            crew.record.update(status="dismissed", startup_error=str(exc))
            crew.session.meta["crew"][crew.crew_id] = crew.record
            write_json(crew.session.meta_path, crew.session.meta)
        raise


def launch_background(crew, current, project, provider, binary, model, args):
    """Start a paneless crew. The roster entry is written before the assignment is
    reserved, so a recruiter that dies at any later point leaves a crew status shows
    (failed, if nothing ran), wait reconciles, and dismiss can retire."""
    task = args.task
    record = {
        **crew.record,
        "provider": provider,
        "execution": "background",
        "model": model,
        "task": task,
        "status": "starting",
    }
    crew.record = record
    current.meta["crew"][crew.crew_id] = record
    write_json(current.meta_path, current.meta)
    try:
        # Held before begin publishes the assignment, so a wait in between sees a live
        # recruiter rather than a lost run.
        with background.launching(crew):
            assignment = begin_recruitment(crew, project, args)
            record["assignment_id"] = assignment["id"]
            write_json(current.meta_path, current.meta)
            files = background.prepare(
                crew, project, provider, binary, model, task, assignment["id"]
            )
            background.start(files)
    except (CaptainError, OSError) as exc:
        try:
            with protocol.checkpoint(current.directory, timeout=background.LOCK_WAIT) as state:
                assignment = protocol.active(state, crew)
                if assignment["state"] != "done":
                    background.close(assignment, "failed", f"[background-start] {exc}")
        except CaptainError:
            pass  # nothing reserved, or the lock stayed held: the next wait reconciles it
        record.update(status="dismissed", startup_error=str(exc))
        write_json(current.meta_path, current.meta)
        raise
    record["status"] = "started"
    write_json(current.meta_path, current.meta)
    add_memory(current.graph, f"session:{current.meta['id']}", "crew", record["agent"])
    add_memory(current.graph, record["agent"], "name", record["name"])
    add_memory(current.graph, record["agent"], "assigned", task)
    print(json.dumps({key: value for key, value in record.items() if key != "task"}))


def unreserved(crew):
    """Whether a background recruit died before `protocol.begin` reserved its assignment."""
    if not crew.is_background:
        return False
    assignment = protocol.active_assignment(crew)
    return not assignment or assignment["incarnation_id"] != crew.record.get("incarnation_id")


def refuse_background(crew, action):
    if crew.is_background:
        raise CaptainError(
            f"[background-unsupported] {crew.display_name} runs in the background with no pane "
            f"and no conversation to continue; {action} is not supported for it."
        )


def choose_checkout(args, project, directory, profile_source=None):
    """The crew checkout and why: explicit choices win, auto isolates only contention."""
    explicit = getattr(args, "checkout", None)
    checkout = explicit or config.text("crew", "checkout")
    if checkout not in ("auto", "shared", "worktree"):
        raise CaptainError("[crew] checkout must be auto, shared or worktree.")
    git = Path(project) / ".git"
    if explicit:
        source = profile_source or "--checkout"
    else:
        where = config.source("crew", "checkout")
        source = f"[crew] checkout from {where}" if where else "[crew] checkout"
    owns = protocol.owned_paths(project, args.owns or ())
    if any(Path(path).is_absolute() for path in owns):
        if checkout == "worktree":
            raise CaptainError(
                "[worktree-outside] A worktree cannot isolate owned paths outside the checkout; "
                "use --checkout shared."
            )
        return "shared", f"{source}: owns paths outside the checkout"
    if not config.flag("crew", "isolation_enabled"):
        if checkout == "worktree":
            raise CaptainError(
                "[worktree-disabled] Crew isolation is disabled; use --checkout shared or "
                "set [crew] isolation_enabled = true."
            )
        return "shared", f"{source}: isolation disabled"
    if not git.exists():
        if checkout == "worktree":
            raise CaptainError("[worktree-required] Isolated crew requires a Git checkout.")
        return "shared", f"{source}: not a Git checkout"
    if checkout == "auto":
        writes = set(args.allow) & set(protocol.WRITE_ACTIONS)
        if args.handoff:
            checkout, reason = "shared", "handoff keeps shared (worktrees cannot hand off)"
        elif not writes:
            checkout, reason = "shared", "read-only crew"
        elif not args.owns:
            checkout, reason = "worktree", "write grant without owned paths"
        elif other := protocol.occupant(project, directory, args.owns):
            checkout = "worktree"
            reason = f"owns overlap active assignment {other['id']} ({other['crew']})"
        else:
            checkout, reason = "shared", "owns disjoint from active crew"
        source = f"{source} auto: {reason}"
    if checkout == "worktree" and (git.is_symlink() or not git.is_dir()):
        raise CaptainError(
            "[unsupported-checkout] Isolated crew currently requires an ordinary Git checkout; "
            "linked captain checkouts are unsupported. Use --checkout shared explicitly."
        )
    return checkout, source


def create_crew(args, pane, project):
    given = args.name
    if given is not None:
        args.name = given.lower()
    if args.name is not None and (
        len(args.name) > 16
        or not re.fullmatch(rf"({'|'.join(CREW_NAMES)})(-[1-9][0-9]*)?", args.name)
    ):
        raise CaptainError(
            f"'{given}' is not on the crew roster. Use a Pirates of the Caribbean "
            "character name (e.g. jack or gibbs), "
            "or omit NAME to assign one automatically. Names must be at most 16 characters."
        )
    check_text(args.task, "task")
    args, profile_sources = profiles.apply(args)
    paneless = getattr(args, "background", False)
    if paneless:
        background.refuse_flags(args)
    current = session(project, pane, args.session)
    managed = is_durable(current.directory, project)
    if paneless and not managed:
        raise CaptainError(
            "[legacy-session] This session lives in temporary storage from an older captain, "
            "so a background run could not be recovered. Start a new captain session."
        )
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        # Under the crew lock, so a same-session recruit cannot slip into the gap.
        checkout, checkout_source = choose_checkout(
            args, project, current.directory, profile_sources.get("checkout")
        )
        if "checkout" in profile_sources:
            profile_sources["checkout"] = checkout_source
        if not managed and checkout == "shared":
            print(
                "captain: legacy shared crew uses an unmanaged launch; native-history cleanup is "
                "unavailable. Start a new captain session for managed cleanup.",
                file=sys.stderr,
            )
        if paneless:
            provider, model, sources = launch_choices(args, current.directory)
            background.check_provider(provider)
            sources["execution"] = "--background"
        else:
            board = dashboard_pane(current)
            rec = Placement(pane, current, board).resolve(args, current.directory)
            provider, placement, spot, model = rec.agent, rec.placement, rec.spot, rec.model
            direction, split_pane, tab_id, auto = spot.direction, spot.pane, spot.tab, spot.reason
            sources = rec.sources
        sources["checkout"] = checkout_source
        recruitment = {}
        if getattr(args, "profile", None) is not None:
            sources.setdefault(
                "execution", "--foreground" if args.foreground else "existing foreground default"
            )
            sources.update(profile_sources)
            effective = {
                "agent": provider,
                "model": model,
                "execution": "background" if paneless else "foreground",
                "checkout": checkout,
            }
            if not paneless:
                effective.update(placement=placement, direction=direction, split_pane=split_pane)
                sources["direction"] = "--direction" if args.direction else "automatic placement"
                sources["split_pane"] = "--split-pane" if args.split_pane else "automatic placement"
            recruitment = {"profile": args.profile, "effective": effective, "sources": sources}
            print(
                f"Recruitment profile: {json.dumps(recruitment, sort_keys=True)}", file=sys.stderr
            )
        print(f"Recruitment sources: {json.dumps(sources, sort_keys=True)}", file=sys.stderr)
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
            {
                "id": name,
                "name": display_name,
                "agent": crew_agent,
                "incarnation_id": incarnation,
                "checkout": checkout,
                "provider": provider,
                **recruitment,
            },
            current,
        )
        if paneless:
            launch_background(crew, current, project, provider, binary, model, args)
            return
        assignment = begin_recruitment(crew, project, args)
        # Reserve ownership before launch; failures before native startup release it below.
        crew.record.update(
            assignment_id=assignment["id"],
            provider=provider,
            task=args.task,
            status="needs_attention",
            outside_dirs=protocol.outside_dirs(assignment["execution_root"], assignment["paths"]),
        )
        meta["crew"][name] = crew.record
        write_json(current.meta_path, meta)
        try:
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
            execution_root = execution.root(current, crew.record)
            if crew.record.get("worktree_id"):
                environment.extend(["--env", f"CAPTAIN_WORKTREE_ID={crew.record['worktree_id']}"])
            else:
                environment.extend(["--env", "CAPTAIN_WORKTREE_ID="])
            history = (
                native_history.prepare(crew, incarnation=incarnation, project=project)
                if managed
                else {"argv": [], "env": {}}
            )
            for key, value in history["env"].items():
                environment.extend(["--env", f"{key}={value}"])
            instruction_text = instructions.agent_instructions(
                current.directory, f"crew member {display_name}", provider
            )
            events = crew.events
            private_dir(events.parent)
            events.write_text("", encoding="utf-8")
            events.chmod(0o600)
            native_argv = [
                binary,
                *instructions.native_args(
                    provider,
                    instruction_text,
                    model,
                    events,
                    add_dirs=crew.record["outside_dirs"],
                ),
                *history["argv"],
            ]
            command = shlex.join(native_argv)
            if managed:
                overrides = dict(value.split("=", 1) for value in environment[1::2])
                overrides.pop("CAPTAIN_CREW_LAUNCHER", None)
                ref = lifetime.prepare(current, crew.record, native_argv, overrides, execution_root)
                crew.record["lifetime_id"] = ref
                write_json(current.meta_path, meta)
                command = shlex.join(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from pathlib import Path; from captain_barbossa.lifetime import run; "
                        "sys.exit(run(Path(sys.argv[1]), sys.argv[2]))",
                        str(current.directory),
                        ref,
                    ]
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
                    str(execution_root),
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
                        "--cwd",
                        str(execution_root),
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
                if new_pane:
                    try:
                        Placement(pane, current, board).even_new_pane(spot, new_pane)
                    except HERDR_ERRORS as exc:
                        print(f"captain: tab not evened: {exc}", file=sys.stderr)
            if not new_pane:
                raise CaptainError(
                    "Herdr created a layout but returned no pane ID. Inspect the workspace before retrying."
                )
        except HERDR_ERRORS as exc:
            # No native task was delivered, so release ownership but retain the failure record.
            with protocol.checkpoint(current.directory) as state:
                failed = protocol.active(state, crew)
                failed.update(state="done", report=f"Recruitment failed before launch: {exc}")
            crew.record.update(status="dismissed", startup_error=str(exc))
            write_json(current.meta_path, meta)
            raise
        record = {
            **crew.record,
            **recruitment,
            "id": name,
            "incarnation_id": incarnation,
            "assignment_id": assignment["id"],
            "name": display_name,
            "agent": crew_agent,
            "provider": provider,
            "pane": new_pane,
            "tab": tab_id,
            "workspace": pane["workspace_id"],
            "terminal_id": created.get("root_pane" if placement == "tab" else "pane", {}).get(
                "terminal_id"
            ),
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
            if managed:
                if not record["terminal_id"]:
                    actual = runtime.herdr("pane", "get", new_pane).get("pane", {})
                    if (
                        actual.get("pane_id") != new_pane
                        or actual.get("workspace_id") != pane["workspace_id"]
                        or actual.get("tab_id") != tab_id
                    ):
                        raise CaptainError(
                            "Herdr returned an unexpected response for herdr pane get; cannot verify crew terminal identity."
                        )
                    record["terminal_id"] = actual.get("terminal_id")
                    write_json(current.meta_path, meta)
                lifetime.bind(current, ref, record)
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
            message_id = protocol.deliver(
                crew, args.task, assignment_id=assignment["id"], initial=True, queue_only=True
            )
            runtime.herdr(
                "pane", "run", new_pane, '/bin/sh "$CAPTAIN_CREW_LAUNCHER"', expect_output=False
            )
            crew.pane.wait_for_crew(new_pane, provider)
            protocol.ring(crew, message_id)
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
