"""Command-line arguments and dispatch for the captain command."""

import argparse
import os
import subprocess
import sys

from . import __version__
from .agents import create_crew, dismiss_crew, focus_crew, launch
from .memory import memory, project_root
from .runtime import CaptainError, current_pane


def parser():
    root = argparse.ArgumentParser(
        prog="captain", description="Native captain and crew for Herdr workspaces."
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument(
        "--agent", choices=("codex", "claude"), help="captain CLI (asks when omitted)"
    )
    root.add_argument(
        "--session",
        default=os.environ.get("CAPTAIN_SESSION"),
        help="reuse graph memory for this session",
    )
    root.add_argument("--prompt", help="initial captain prompt")
    commands = root.add_subparsers(dest="command")
    crew = commands.add_parser("crew", help="create a native crew after agent and pane/tab choices")
    crew.add_argument(
        "name", nargs="?", help="Pirates character name (automatically assigned when omitted)"
    )
    crew.add_argument(
        "--agent",
        dest="crew_agent",
        choices=("codex", "claude"),
        help="crew CLI (asks when omitted)",
    )
    crew.add_argument("--task", required=True)
    crew.add_argument(
        "--placement", choices=("pane", "tab"), help="the placement explicitly chosen by the user"
    )
    crew.add_argument(
        "--direction",
        choices=("vertical", "horizontal", "auto"),
        help="pane split direction chosen by the user, or auto (asks when omitted)",
    )
    crew.add_argument(
        "--split-pane",
        help="pane ID anywhere in the workspace to split, or auto to pick pane and direction "
        "from the current tab's layout (asks when omitted)",
    )
    crew.add_argument("--model", help="model name or alias, matched to the crew CLI's models")
    focus = commands.add_parser("focus", help="focus an existing crew's pane and tab")
    focus.add_argument("name", help="crew name or ID (case-insensitive)")
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
    actions.add_parser("path", help="print this session's memory directory")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        pane = current_pane()
        project = project_root()
        if args.command == "crew":
            create_crew(args, pane, project)
        elif args.command == "focus":
            focus_crew(args, pane, project)
        elif args.command == "dismiss":
            dismiss_crew(args, pane, project)
        elif args.command == "memory":
            memory(args, pane, project)
        else:
            launch(args, pane, project)
    except (CaptainError, OSError, subprocess.TimeoutExpired, EOFError) as exc:
        print(f"captain: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
