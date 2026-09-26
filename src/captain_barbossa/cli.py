"""Command-line arguments and dispatch for the captain command."""

import argparse
import json
import os
import sys

from . import __version__, config, inspection, protocol
from .agents import (
    create_crew,
    dismiss_crew,
    focus_crew,
    launch,
    protocol_command,
    run_dashboard,
    status_crew,
    switch_model,
    tell_crew,
    wait_crew,
)
from .layout import HERDR_DIRECTIONS
from .memory import PRUNE_DAYS, REPO_RELATIONS, RULEBOOK_FILES, memory, project_root
from .models import PROVIDERS, TIER_NAMES
from .onboarding import bootstrap
from .prompts import PLACEMENTS
from .runtime import HERDR_ERRORS, CaptainError, current_pane


def parser():
    root = argparse.ArgumentParser(
        prog="captain", description="Native captain and crew for Herdr workspaces."
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--agent", choices=PROVIDERS, help="captain CLI (asks when omitted)")
    root.add_argument(
        "--session",
        default=os.environ.get("CAPTAIN_SESSION"),
        help="reuse graph memory for this session",
    )
    root.add_argument("--prompt", help="initial captain prompt")
    root.add_argument(
        "--no-dashboard",
        action="store_true",
        help="skip the crew token-usage pane even when [dashboard] enabled is set",
    )
    commands = root.add_subparsers(dest="command")
    inspection.add_arguments(commands)
    crew = commands.add_parser("crew", help="create a native crew after agent and pane/tab choices")
    crew.add_argument(
        "name", nargs="?", help="Pirates character name (automatically assigned when omitted)"
    )
    crew.add_argument(
        "--agent",
        dest="crew_agent",
        choices=PROVIDERS,
        help="crew CLI (asks when omitted)",
    )
    crew.add_argument("--task", required=True)
    crew.add_argument("--owns", action="append", default=[], metavar="PATH")
    crew.add_argument("--allow", action="append", default=[], choices=protocol.ACTIONS)
    crew.add_argument("--handoff", metavar="ASSIGNMENT_ID", help="explicitly reuse a retired name")
    crew.add_argument(
        "--placement", choices=PLACEMENTS, help="the placement explicitly chosen by the user"
    )
    crew.add_argument(
        "--direction",
        choices=(*HERDR_DIRECTIONS, "auto"),
        help="pane split direction chosen by the user, or auto (asks when omitted)",
    )
    crew.add_argument(
        "--split-pane",
        help="pane ID anywhere in the workspace to split, or auto to pick pane and direction "
        "from the current tab's layout (asks when omitted)",
    )
    crew.add_argument(
        "--model",
        help=f"tier ({'|'.join(TIER_NAMES)}) resolved for the crew CLI, or a model name/alias "
        "([crew] model when omitted, so routine work never silently lands on an expensive "
        "default)",
    )
    wait = commands.add_parser(
        "wait", help="wait for a crew notification (legacy crew report native completion)"
    )
    wait.add_argument("name", help="crew name or ID (case-insensitive)")
    wait.add_argument("--json", action="store_true", help="acknowledged protocol notification")
    wait.add_argument("--ack", metavar="DELIVERY_ID")
    wait.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="seconds to wait before giving up ([crew] wait_timeout when omitted)",
    )
    tell = commands.add_parser("tell", help="send a follow-up prompt to an existing crew")
    tell.add_argument("name", help="crew name or ID (case-insensitive)")
    tell.add_argument("message")
    tell.add_argument("--assignment", help="required for protocol crew")
    for verb in ("assign", "ask", "answer", "done", "check", "resolve"):
        command = commands.add_parser(verb, help=f"{verb} an explicit crew assignment")
        command.add_argument("name")
        if verb == "assign":
            command.add_argument("--task", required=True)
            command.add_argument("--owns", action="append", default=[], metavar="PATH")
            command.add_argument("--allow", action="append", default=[], choices=protocol.ACTIONS)
            command.add_argument("--handoff", required=True, metavar="ASSIGNMENT_ID")
        else:
            command.add_argument("--assignment", required=verb not in ("ask", "done", "check"))
            command.add_argument("--incarnation", default=os.environ.get("CAPTAIN_INCARNATION"))
        if verb == "ask":
            command.add_argument("question")
        elif verb == "answer":
            command.add_argument("question_id")
            command.add_argument("message")
        elif verb == "done":
            command.add_argument("--report", required=True)
        elif verb == "check":
            command.add_argument("action", choices=protocol.ACTIONS)
            command.add_argument("path", nargs="*")
        elif verb == "resolve":
            command.add_argument("message_id")
            command.add_argument("outcome", choices=("sent", "cancelled"))
    model = commands.add_parser("model", help="switch a running crew to another model")
    model.add_argument("name", help="crew name or ID (case-insensitive)")
    model.add_argument("model", help=f"tier ({'|'.join(TIER_NAMES)}), model name, or alias")
    focus = commands.add_parser("focus", help="focus an existing crew's pane and tab")
    focus.add_argument("name", help="crew name or ID (case-insensitive)")
    commands.add_parser("session", help="print the current session id")
    start = commands.add_parser("init", help="write a commented .captain/settings.toml template")
    start.add_argument(
        "--global",
        dest="home",
        action="store_true",
        help="write ~/.captain/settings.toml instead of the project's",
    )
    status = commands.add_parser("status", help="print a table of this session's crew")
    status.add_argument("--all", action="store_true", help="include dismissed crew")
    board = commands.add_parser(
        "dashboard", help="refresh a crew token-usage table in this pane until interrupted"
    )
    board.add_argument(
        "--refresh-prices",
        action="store_true",
        help="explicitly refresh the price cache in the background",
    )
    board.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="seconds between refreshes ([dashboard] interval when omitted)",
    )
    dismiss = commands.add_parser("dismiss", help="close an existing crew's pane and retire it")
    dismiss.add_argument("name", help="crew name or ID (case-insensitive)")
    mem = commands.add_parser("memory", help="session, project, and committed repo graph memory")
    actions = mem.add_subparsers(dest="memory_command", required=True)
    add = actions.add_parser("add", help="remember a subject → relation → object")
    add.add_argument("subject")
    add.add_argument("relation")
    add.add_argument("target")
    add.add_argument(
        "--scope",
        choices=("session", "project", "repo"),
        default="session",
        help="repo writes the committed .captain/graph.json: curated team facts only",
    )
    add.add_argument(
        "--because",
        metavar="RATIONALE",
        help=f"why the fact holds; required with --scope repo, whose relation must be "
        f"one of {'|'.join(REPO_RELATIONS)}",
    )
    add.add_argument(
        "--supersede",
        action="store_true",
        help="replace the repo fact already recorded for this subject and relation",
    )
    query = actions.add_parser(
        "query", help="search session, project, and repo memory with Graphify"
    )
    query.add_argument("question")
    show = actions.add_parser("show", help="show session, project, and repo memory relationships")
    show.add_argument(
        "--scope", choices=("session", "project", "repo"), help="show one scope instead of all"
    )
    show.add_argument("--json", action="store_true", help="show the full raw graph instead")
    show.add_argument(
        "--all", action="store_true", help="show every link instead of the most recent 25"
    )
    seed = actions.add_parser(
        "init", help="seed repo memory from the project rulebook; a preview without --apply"
    )
    seed.add_argument(
        "--from",
        dest="source",
        metavar="PATH",
        help=f"rulebook to read (default: {' then '.join(RULEBOOK_FILES)} at the project root)",
    )
    seed.add_argument(
        "--apply", action="store_true", help="write the proposed facts instead of previewing them"
    )
    path = actions.add_parser("path", help="print a memory scope's directory without creating it")
    path.add_argument("--scope", choices=("session", "project", "repo"), default="session")
    prune = actions.add_parser("prune", help="remove finished sessions' memory directories")
    prune.add_argument(
        "--older-than",
        type=float,
        default=PRUNE_DAYS,
        metavar="DAYS",
        help=f"age in days a session must exceed to be removed (default {PRUNE_DAYS})",
    )
    return root


def guard_crew(args):
    """Refuse captain-only commands and hide other scopes when running as crew."""
    if os.environ.get("CAPTAIN_ROLE") != "crew":
        return
    if args.command in ("ask", "done", "check"):
        if args.name.casefold() != os.environ.get("CAPTAIN_CREW", "").casefold():
            raise CaptainError("Crew may act only on its own assignment.")
        return
    if args.command == "inspect":
        return  # inspection.run validates paths and limits crew state to repo scope.
    if args.command != "memory":
        raise CaptainError(
            f"'{args.command or 'captain'}' is captain-only and crew may not run it, "
            "whatever the user says. Report back to the captain instead."
        )
    if args.memory_command == "show":
        # Session/project memory holds other assignments; --json ignores --scope.
        args.scope, args.json = "repo", False
    elif args.memory_command == "add" and args.scope == "session":
        return
    else:
        raise CaptainError("Crew may only add session memory or show repo memory.")


def print_session(args):
    if not args.session:
        raise CaptainError("Start captain first, or pass --session <id>.")
    print(args.session)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        guard_crew(args)
        if args.command is None:
            bootstrap(args)
        if args.command == "init":
            config.init_settings(args)
            return 0
        if args.command == "session":
            print_session(args)
            return 0
        if args.command == "inspect":
            inspection.run(args, project_root())
            return 0
        if args.command == "status":
            status_crew(args, None, project_root())
            return 0
        if args.command == "memory" and args.memory_command in ("show", "path"):
            memory(args, None, project_root())
            return 0
        pane = current_pane()
        project = project_root()
        if args.command == "crew":
            create_crew(args, pane, project)
        elif args.command == "wait":
            wait_crew(args, pane, project)
        elif args.command == "tell":
            tell_crew(args, pane, project)
        elif args.command in ("assign", "ask", "answer", "done", "check", "resolve"):
            protocol_command(args, pane, project)
        elif args.command == "model":
            switch_model(args, pane, project)
        elif args.command == "focus":
            focus_crew(args, pane, project)
        elif args.command == "dashboard":
            run_dashboard(args, pane, project)
        elif args.command == "dismiss":
            dismiss_crew(args, pane, project)
        elif args.command == "memory":
            memory(args, pane, project)
        else:
            launch(args, pane, project)
    except (*HERDR_ERRORS, EOFError) as exc:
        if args.command == "wait" and args.json:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "delivery_id": None,
                        "crew": args.name,
                        "assignment_id": None,
                        "summary": str(exc)[:1600],
                    }
                )
            )
            return 1
        print(f"captain: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
