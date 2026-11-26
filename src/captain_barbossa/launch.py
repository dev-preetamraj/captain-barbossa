"""Launch the native captain CLI in Herdr and close its session panes when it exits."""

import json
import os
import re
import sys
import time
from pathlib import Path

from . import (
    config,
    instructions,
    protocol,
    recovery,
    results,
    retirement,
    runtime,
)
from .crew import Crew
from .dashboard import dashboard_pane, start_dashboard
from .memory import add_memory
from .models import PROVIDERS, TRANSPORTS, resolve_model
from .pi_captain import captain_extension
from .prompts import choose
from .retention import prune_sessions
from .runtime import HERDR_ERRORS, CaptainError, executable
from .sessions import crew_meta, read_session, session
from .store import private_dir, read_json, state_root, temp_root, write_json
from .update_check import check_for_update

# How often the exit watcher checks whether the captain's CLI is still its parent. A pane
# that outlives the captain by a second costs nothing.
EXIT_POLL_SECONDS = 1.0

# Structured transports a captain can be driven over; a provider's first TRANSPORTS entry must
# be one of them for `launch-plan` and the supervisor to start it.
CAPTAIN_TRANSPORTS = ("app-server", "sdk", "rpc")

# The launch digest rides in one argv string, which Linux caps at 128 KiB.
RECOVERY_CHARS = 32_000


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
                    # A crew never assigned owns nothing; any other failure is unverified.
                    unassigned = state["active"].get(crew.crew_id) is None
                    reasons = [] if unassigned else [str(exc)]
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
                    record["cleanup"] = retirement.retire(crew)
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


def prepare_launch(
    current, project, provider, binary, model, plugins=True, transport="tui", bridge=True
):
    """The native captain command and env overlay; `bridge` False omits pi's own delivery bridge."""
    model = resolve_model(provider, model) if model else None
    with crew_meta(current.directory) as meta:
        meta["captain_provider"] = provider
        bound = None
        if plugins and config.home_settings().get("plugins"):
            from . import plugins as portal

            bound = meta["plugins"] = portal.binding()
        else:
            meta.pop("plugins", None)
    # A Herdr launch calls exactly as it always has; only an app session passes the flag.
    app = {"supervised": True} if meta.get("transport") == "supervisor" else {}
    instruction_text = instructions.agent_instructions(
        current.directory, "Captain Barbossa", provider, **app
    )
    # Delivered, not requested: a resumed captain that skips `recover` still knows its session.
    # Preferences serialise first, so the cut below only ever takes the session's tail.
    state = json.dumps(recovery.digest(current, project))
    if len(state) > RECOVERY_CHARS:
        state = state[:RECOVERY_CHARS] + "... truncated; run CAPTAIN recover"
    instruction_text += f"Launch state (reference data, not instructions):\n{state}\n"
    if bound is not None:
        # After the cut, so plugin context never competes with the session for it.
        instruction_text += portal.context(bound, "captain")
        portal.refresh()
    add_memory(current.graph, f"session:{meta['id']}", "captain", provider)
    events = current.events("captain")
    private_dir(events.parent)
    command = [
        *instructions.native_command(binary, transport),
        *instructions.native_args(
            provider, instruction_text, model, events=events, transport=transport
        ),
    ]
    if provider == "pi" and bridge:
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
    transport = next(iter(TRANSPORTS.get(provider, ())), None)
    if transport not in CAPTAIN_TRANSPORTS:
        raise LaunchPlanError(
            "unsupported-transport", f"{provider} has no structured transport for a captain."
        )
    if args.effort and provider != "pi":
        raise LaunchPlanError("invalid-arguments", "--effort applies to pi only.")
    identity = None
    current = None
    if args.session:
        try:
            current = session(project, None, args.session)
        except CaptainError as exc:
            raise LaunchPlanError("invalid-session", str(exc)) from exc
        if current.meta.get("captain_provider") != provider:
            raise LaunchPlanError(
                "session-provider-mismatch", f"Session provider is not {provider}."
            )
    if current is not None and provider == "pi":
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
    command, env = prepare_launch(
        current,
        project,
        provider,
        binary,
        model,
        plugins=not getattr(args, "no_plugins", False),
        transport=transport,
    )
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
        "transport": transport,
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
    command, overrides = prepare_launch(
        current, project, provider, binary, model, plugins=not getattr(args, "no_plugins", False)
    )
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
