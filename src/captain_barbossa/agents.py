"""Launch native captain and crew sessions in Herdr."""

import json
import os
import re
import shlex
import subprocess
import sys
from itertools import cycle

from . import instructions, runtime
from .crew import WAIT_TIMEOUT as WAIT_TIMEOUT
from .crew import Crew
from .memory import (
    add_memory,
    crew_meta,
    private_dir,
    prune_sessions,
    read_events,
    read_json,
    session,
    state_root,
    temp_root,
    truncate_label,
    write_json,
)
from .models import model_names, resolve_model
from .pane import MODEL_TIMEOUT, PROMPT_TIMEOUT
from .placement import Placement
from .prompts import choose
from .runtime import CaptainError, executable

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


def launch(args, pane, project):
    if not sys.stdin.isatty():
        raise CaptainError("Launch captain from an interactive Herdr terminal.")
    provider = choose(args.agent, ("claude", "codex"), "Choose your captain", "--agent")
    binary = executable(provider)
    current = session(project, pane, args.session, create=args.session is None)
    meta = current.meta
    try:
        prune_sessions(project, current=meta["id"])
    except (CaptainError, OSError, subprocess.TimeoutExpired):
        pass  # retention is housekeeping; never block a launch on it
    instruction_text = instructions.agent_instructions(current.directory, "Captain Barbossa")
    write_json(
        current.directory / "captain.json",
        {"provider": provider, "pane": pane["pane_id"], "terminal_id": pane.get("terminal_id")},
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
    command = [binary, *instructions.native_args(provider, instruction_text)]
    if args.prompt:
        command.extend(["--", args.prompt])
    os.execvpe(binary, command, env)


def wait_crew(args, pane, project):
    current = session(project, pane, args.session)
    crew = Crew.resolve(current, args.name)
    display_name = crew.display_name
    events = crew.events
    report_cursor = events.with_suffix(".reports")
    reported = read_json(report_cursor) if report_cursor.exists() else 0
    # Legacy pane detection has no event boundary; only trust reports written during wait.
    before = len(crew.reports)
    status, event = crew.status(max(args.timeout, 0))
    if status is None:
        raise CaptainError(
            f"{display_name} was still working after {args.timeout} seconds. "
            "Wait again, or read its pane with: herdr agent read " + crew.record["agent"]
        )
    all_reports = crew.reports
    reports = all_reports[reported if event is not None else before :]
    if reports:
        entry = f"{status}; reported: {reports[-1]}"
        # Blocked means the pane is waiting on input/approval; show it even with a report.
        printed = (
            f"{entry}; pane tail: {crew.pane.tail()}"
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
        # Kept short: the full tail already went to stdout, this is just a debugging breadcrumb.
        add_memory(current.graph, display_name, "tail", truncate_label(tail))
        entry = f"{status}; no report recorded"
        printed = f"{entry}; pane tail: {tail}"
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
            printed += f"; hook: {message}"
    add_memory(current.graph, display_name, "completed", completed[:8000])
    if events.exists():
        write_json(report_cursor, len(all_reports))
    print(f"{display_name} {status}.")
    print(printed)


def focus_crew(args, pane, project):
    current = session(project, pane, args.session)
    crew = Crew.resolve(current, args.name)
    display_name = crew.display_name
    try:
        agent = runtime.herdr("agent", "get", crew.record["agent"]).get("agent", {})
        # The live agent wins over the recorded tab when its pane has moved.
        tab_id = agent.get("tab_id") or crew.record.get("tab")
        if tab_id and tab_id != pane["tab_id"]:
            runtime.herdr("tab", "focus", tab_id)
        runtime.herdr("agent", "focus", crew.record["agent"])
    except CaptainError as exc:
        raise CaptainError(f"Could not focus {display_name}: {exc}") from exc
    print(f"Focused {display_name}.")


def status_crew(args, pane, project):
    """Print a table of this session's crew, refreshing status from Herdr best-effort."""
    current = session(project, pane, args.session)
    rows = []
    for crew in Crew.members(current):
        if crew.is_dismissed and not args.all:
            continue
        status = crew.record.get("status") or "-"
        try:
            agent = runtime.herdr("agent", "get", crew.record["agent"], timeout=5).get("agent", {})
            status = agent.get("agent_status") or status
        except (CaptainError, subprocess.TimeoutExpired, OSError):
            pass
        task = (crew.record.get("task") or "").splitlines()
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
    if not args.message.strip() or len(args.message) > 8000 or "\x00" in args.message:
        raise CaptainError("Provide a message of 1-8000 characters, without NUL bytes.")
    current = session(project, pane, args.session)
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        crew = Crew.resolve(current, args.name)
        display_name = crew.display_name
        if crew.is_dismissed:
            raise CaptainError(f"{display_name} was dismissed; recruit new crew instead.")
        events = crew.events
        cursor = events.with_suffix(".cursor")
        # An idle event from the gap before this prompt would otherwise read as done.
        _, offset = read_events(events, read_json(cursor) if cursor.exists() else 0)
        if events.exists():
            write_json(cursor, offset)
        crew.record["task"] = args.message
        crew.pane.submit_task(args.message, crew.record.get("provider"))
        add_memory(current.graph, display_name, "assigned", args.message)
    print(f"Sent to {display_name}.")


def switch_model(args, pane, project):
    """Switch a running crew to another model through the native CLI's own /model command."""
    current = session(project, pane, args.session)
    crew = Crew.resolve(current, args.name)
    display_name = crew.display_name
    provider = crew.record["provider"]
    model = resolve_model(provider, args.model)
    agent_name = crew.record["agent"]
    names = [name.casefold() for name in model_names(provider, model)]
    if provider == "claude":
        runtime.herdr("agent", "prompt", agent_name, f"/model {model}")
        if not crew.pane.model_landed(names, PROMPT_TIMEOUT):
            # Claude Code can leave a submitted line as an unsent draft in its input box.
            runtime.herdr("agent", "send-keys", agent_name, "enter")
    else:
        runtime.herdr("agent", "prompt", agent_name, "/model")
        crew.pane.codex_pick_model(model, MODEL_TIMEOUT)
    if not crew.pane.model_landed(names, MODEL_TIMEOUT):
        raise CaptainError(
            f"{display_name} did not confirm the switch to {model}. "
            f"Read its pane before retrying: herdr agent read {agent_name}"
        )
    with crew_meta(current.directory) as meta:
        meta["crew"][crew.crew_id]["model"] = model
    add_memory(current.graph, display_name, "model", model)
    print(f"{display_name} switched to {model}.")
    if provider == "claude":
        # Claude Code's inline /model always writes the model to the user's settings.
        print("Claude Code also saved it as the default for new sessions.")


def dismiss_crew(args, pane, project):
    current = session(project, pane, args.session)
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        crew = Crew.resolve(current, args.name)
        display_name = crew.display_name
        if crew.is_dismissed:
            raise CaptainError(f"{display_name} was already dismissed.")
        if not crew.record.get("pane"):
            raise CaptainError(f"{display_name} has no recorded pane to close.")
        try:
            runtime.herdr("pane", "close", crew.record["pane"])
        except CaptainError as exc:
            raise CaptainError(f"Could not dismiss {display_name}: {exc}") from exc
        (current.directory / f"crew-{crew.crew_id}.sh").unlink(missing_ok=True)
        crew.record["status"] = "dismissed"
        tab_id = crew.record.get("tab")
        label = Crew.tab_label(current, tab_id) if tab_id and tab_id != pane["tab_id"] else None
    # Rename after the roster write: a stale tab id must not cost the dismissal record.
    if label:
        runtime.herdr("tab", "rename", tab_id, label)
    add_memory(current.graph, f"session:{meta['id']}", "dismissed", crew.record["agent"])
    print(f"Dismissed {display_name}.")


def create_crew(args, pane, project):
    if args.name is not None and (
        len(args.name) > 16
        or not re.fullmatch(rf"({'|'.join(CREW_NAMES)})(-[1-9][0-9]*)?", args.name)
    ):
        raise CaptainError(
            "Use a Pirates of the Caribbean character name (e.g. jack or gibbs), "
            "or omit NAME to assign one automatically. Names must be at most 16 characters."
        )
    if not args.task.strip() or len(args.task) > 8000 or "\x00" in args.task:
        raise CaptainError("Provide a task of 1–8000 characters, without NUL bytes.")
    provider = choose(args.crew_agent, ("claude", "codex"), "Choose your crew agent", "--agent")
    placement = choose(
        args.placement, ("pane", "tab"), "Where should the crew open?", "--placement"
    )
    current = session(project, pane, args.session)
    with crew_meta(current.directory) as meta:
        current = current._replace(meta=meta)
        direction, split_pane, tab_id, auto = Placement(pane, meta).choose_split(args, placement)
        if auto and split_pane is None:
            placement = "tab"
        model = resolve_model(provider, args.model) if args.model is not None else None
        if model:
            print(f"Model: {model} (from {args.model!r})", file=sys.stderr)
        binary = executable(provider)
        name = args.name
        if name is None:
            for index, character in enumerate(cycle(CREW_NAMES)):
                round_number = index // len(CREW_NAMES) + 1
                name = f"{character}-{round_number}" if round_number > 1 else character
                if not Crew.name_reserved(current, name):
                    break
        display_name = name.capitalize()
        if Crew.name_reserved(current, name):
            raise CaptainError(f"Crew '{display_name}' already exists in this session.")
        agent_name = f"c-{meta['id'][:8]}-{name}"
        launcher = current.directory / f"crew-{name}.sh"
        environment = [
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
            current.directory, f"crew member {display_name}"
        )
        crew = Crew(name, {"id": name, "name": display_name, "agent": agent_name}, current)
        events = crew.events
        private_dir(events.parent)
        events.write_text("", encoding="utf-8")
        events.chmod(0o600)
        events.with_suffix(".cursor").unlink(missing_ok=True)
        write_json(
            events.with_suffix(".reports"),
            len(crew.reports),
        )
        command = shlex.join(
            [binary, *instructions.native_args(provider, instruction_text, model, events)]
        )
        launcher.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
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
        if not new_pane:
            raise CaptainError(
                "Herdr created a layout but returned no pane ID. Inspect the workspace before retrying."
            )
        record = {
            "id": name,
            "name": display_name,
            "agent": agent_name,
            "provider": provider,
            "pane": new_pane,
            "tab": tab_id,
            "placement": placement,
            "direction": direction,
            "split_pane": split_pane,
            "auto": auto,
            "model": model,
            "task": args.task,
            "status": "starting",
        }
        crew.record = record
        meta["crew"][name] = record
        write_json(current.meta_path, meta)
        try:
            add_memory(current.graph, f"session:{meta['id']}", "crew", agent_name)
            add_memory(current.graph, agent_name, "name", display_name)
            add_memory(current.graph, agent_name, "assigned", args.task)
            if model:
                add_memory(current.graph, agent_name, "model", model)
            if auto:
                add_memory(current.graph, agent_name, "placement", f"auto: {auto}")
            runtime.herdr("pane", "rename", new_pane, display_name)
            # A new shell may still be in canonical mode: keep terminal input short.
            runtime.herdr(
                "pane", "run", new_pane, '/bin/sh "$CAPTAIN_CREW_LAUNCHER"', expect_output=False
            )
            crew.pane.wait_for_crew(new_pane, provider)
            crew.pane.submit_task(args.task, provider)
            record["status"] = "started"
            if tab_id != pane["tab_id"]:
                label = Crew.tab_label(current, tab_id)
                if label:
                    runtime.herdr("tab", "rename", tab_id, label)
        except (CaptainError, subprocess.TimeoutExpired, OSError) as exc:
            record["status"] = "needs_attention"
            write_json(current.meta_path, meta)
            launcher.unlink(missing_ok=True)
            raise CaptainError(
                f"Crew pane {new_pane} was created but startup needs attention: {exc}. "
                f"Inspect pane {new_pane} in Herdr. The pane was preserved; "
                "the task was not automatically retried."
            ) from exc
    print(json.dumps({key: value for key, value in record.items() if key != "task"}))
