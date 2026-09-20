"""Command-line arguments and dispatch for the captain command."""

import argparse
import os
import sys

from . import __version__
from .agents import (
    WAIT_TIMEOUT,
    create_crew,
    dismiss_crew,
    focus_crew,
    launch,
    run_dashboard,
    status_crew,
    switch_model,
    tell_crew,
    wait_crew,
)
from .layout import HERDR_DIRECTIONS
from .memory import PRUNE_DAYS, memory, project_root
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
        help="do not open the crew token-usage pane below the captain",
    )
    commands = root.add_subparsers(dest="command")
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
        default="cheap",
        help=f"tier ({'|'.join(TIER_NAMES)}) resolved for the crew CLI, or a model name/alias "
        "(default: cheap, so routine work never silently lands on an expensive default)",
    )
    wait = commands.add_parser(
        "wait", help="wait for a crew to finish, then record and print its completion"
    )
    wait.add_argument("name", help="crew name or ID (case-insensitive)")
    wait.add_argument(
        "--timeout",
        type=float,
        default=WAIT_TIMEOUT,
        help=f"seconds to wait before giving up (default {WAIT_TIMEOUT})",
    )
    tell = commands.add_parser("tell", help="send a follow-up prompt to an existing crew")
    tell.add_argument("name", help="crew name or ID (case-insensitive)")
    tell.add_argument("message")
    model = commands.add_parser("model", help="switch a running crew to another model")
    model.add_argument("name", help="crew name or ID (case-insensitive)")
    model.add_argument("model", help=f"tier ({'|'.join(TIER_NAMES)}), model name, or alias")
    focus = commands.add_parser("focus", help="focus an existing crew's pane and tab")
    focus.add_argument("name", help="crew name or ID (case-insensitive)")
    commands.add_parser("session", help="print the current session id")
    status = commands.add_parser("status", help="print a table of this session's crew")
    status.add_argument("--all", action="store_true", help="include dismissed crew")
    board = commands.add_parser(
        "dashboard", help="refresh a crew token-usage table in this pane until interrupted"
    )
    board.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="seconds between refreshes (dashboard default when omitted)",
    )
    dismiss = commands.add_parser("dismiss", help="close an existing crew's pane and retire it")
    dismiss.add_argument("name", help="crew name or ID (case-insensitive)")
    mem = commands.add_parser("memory", help="project/session graph memory outside the repo")
    actions = mem.add_subparsers(dest="memory_command", required=True)
    add = actions.add_parser("add", help="remember a subject → relation → object")
    add.add_argument("subject")
    add.add_argument("relation")
    add.add_argument("target")
    add.add_argument("--scope", choices=("session", "project"), default="session")
    query = actions.add_parser("query", help="search session and project memory with Graphify")
    query.add_argument("question")
    show = actions.add_parser("show", help="show session and project memory relationships")
    show.add_argument("--json", action="store_true", help="show the full raw graph instead")
    show.add_argument(
        "--all", action="store_true", help="show every link instead of the most recent 25"
    )
    actions.add_parser("path", help="print this session's memory directory")
    prune = actions.add_parser("prune", help="remove finished sessions' memory directories")
    prune.add_argument(
        "--older-than",
        type=float,
        default=PRUNE_DAYS,
        metavar="DAYS",
        help=f"age in days a session must exceed to be removed (default {PRUNE_DAYS})",
    )
    return root


def print_session(args):
    if not args.session:
        raise CaptainError("Start captain first, or pass --session <id>.")
    print(args.session)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command is None:
            bootstrap(args)
        pane = current_pane()
        project = project_root()
        if args.command == "crew":
            create_crew(args, pane, project)
        elif args.command == "wait":
            wait_crew(args, pane, project)
        elif args.command == "tell":
            tell_crew(args, pane, project)
        elif args.command == "model":
            switch_model(args, pane, project)
        elif args.command == "focus":
            focus_crew(args, pane, project)
        elif args.command == "session":
            print_session(args)
        elif args.command == "status":
            status_crew(args, pane, project)
        elif args.command == "dashboard":
            run_dashboard(args, pane, project)
        elif args.command == "dismiss":
            dismiss_crew(args, pane, project)
        elif args.command == "memory":
            memory(args, pane, project)
        else:
            launch(args, pane, project)
    except (*HERDR_ERRORS, EOFError) as exc:
        print(f"captain: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
