"""Command-line arguments and dispatch for the captain command."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, config, dashboard, do, events, inspection, protocol, recovery
from .agents import (
    create_crew,
    dismiss_crew,
    focus_crew,
    interrupt_crew,
    protocol_command,
    relayout_crew,
    status_crew,
    switch_model,
    tell_crew,
    wait_crew,
)
from .crew import Crew
from .events import mail_context, receipt_for
from .execution import active_project
from .launch import LaunchPlanError, launch, launch_plan
from .layout import HERDR_DIRECTIONS
from .memory import REPO_RELATIONS, RULEBOOK_FILES, memory
from .models import PROVIDERS, TIER_NAMES, models_for, tiers_for
from .onboarding import bootstrap
from .prompts import PLACEMENTS, pick_session
from .retention import PRUNE_DAYS, archive_session, delete_session
from .retirement import artifact_command
from .retirement import dispose as cleanup_worktree
from .runtime import HERDR_ERRORS, CaptainError, current_pane
from .sessions import (
    catalog,
    find_session,
    project_sessions,
    read_session,
    rename_session,
    session_card,
    set_archived,
)
from .store import project_root
from .update_check import upgrade

# supervision.TOKEN_ENV, named here so a Herdr command never imports the supervisor door.
SUPERVISOR_TOKEN_ENV = "CAPTAIN_SUPERVISOR_TOKEN"
VERIFY_HELP = "Makefile target captain runs at done; its receipt rides the report"
JSON_HELP = "print the outcome as one JSON line instead of text"
# Verbs whose --json failure is a JSON envelope on stdout, {"error": {code, message}}.
JSON_VERBS = ("crew", "tell", "answer", "interrupt", "model", "dismiss", "status", "dashboard")
JSON_VERBS += ("providers", "commands")
ARCHIVE_VERBS = {
    "archive": archive_session,
    "unarchive": lambda directory: set_archived(directory, False),
    "delete": delete_session,
}
REFUSAL_CODE = re.compile(r"\[([a-z0-9-]+)\] ")


class PlanParser(argparse.ArgumentParser):
    def error(self, message):
        raise LaunchPlanError("invalid-arguments", message)


def parser(*, plan=False):
    root = (PlanParser if plan else argparse.ArgumentParser)(
        prog="captain", description="Native captain and crew for Herdr workspaces."
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--agent", choices=PROVIDERS, help="captain CLI (asks when omitted)")
    root.add_argument(
        "--session",
        default=None if plan else os.environ.get("CAPTAIN_SESSION"),
        help="reuse graph memory for this session",
    )
    root.add_argument("--prompt", help="initial captain prompt")
    sessions = root.add_mutually_exclusive_group()
    sessions.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="TARGET",
        help="list sessions, or resume one by name or id",
    )
    sessions.add_argument("--rename", metavar="NAME", help="name the current session")
    root.add_argument(
        "--if-unnamed", action="store_true", help="with --rename, keep an existing name"
    )
    root.add_argument(
        "--no-plugins",
        action="store_true",
        help="launch with no plugin context and no plugin crew denials this session",
    )
    root.add_argument(
        "--no-dashboard",
        action="store_true",
        help="skip the crew token-usage pane even when [dashboard] enabled is set",
    )
    commands = root.add_subparsers(dest="command")
    plan_command = commands.add_parser("launch-plan", help="prepare a pane-free pi RPC captain")
    plan_command.add_argument("--agent", default=argparse.SUPPRESS)
    plan_command.add_argument("--model")
    plan_command.add_argument("--effort")
    plan_command.add_argument("--session", default=argparse.SUPPRESS)
    inspection.add_arguments(commands)
    do.add_arguments(commands)
    result = commands.add_parser("result", help="review or transfer isolated crew work")
    result_actions = result.add_subparsers(dest="result_command", required=True)
    collect = result_actions.add_parser("collect")
    collect.add_argument("worktree_id")
    for action in ("show", "apply", "discard"):
        command = result_actions.add_parser(action)
        command.add_argument("result_id")
        if action == "discard":
            command.add_argument("--reason", required=True)
    plugin = commands.add_parser("plugin", help="register, sync and inspect captain plugins")
    plugin_actions = plugin.add_subparsers(dest="plugin_command", required=True)
    add = plugin_actions.add_parser("add", help="register an executable in home settings")
    add.add_argument("name")
    add.add_argument("executable")
    add.add_argument(
        "--required", action="store_true", help="block captain launch on a version mismatch"
    )
    plugin_actions.add_parser("remove", help="unregister a plugin").add_argument("name")
    for action, text in (
        ("list", "registered plugins and what they negotiated"),
        ("sync", "refresh plugin mirrors now"),
        ("status", "mirror freshness, expiry and last failure as JSON"),
    ):
        plugin_actions.add_parser(action, help=text).add_argument("name", nargs="?")
    worktree = commands.add_parser("worktree", help="retained isolated crew checkouts")
    worktree_actions = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_actions.add_parser("list")
    cleanup = worktree_actions.add_parser("cleanup")
    cleanup.add_argument("worktree_id")
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
    crew.add_argument("--profile", help="recruitment preset from [profiles.NAME] settings")
    crew.add_argument(
        "--checkout", choices=("auto", "shared", "worktree"), help="crew checkout isolation"
    )
    crew.add_argument("--owns", action="append", default=[], metavar="PATH")
    crew.add_argument("--allow", action="append", default=[], choices=protocol.ACTIONS)
    crew.add_argument("--handoff", metavar="ASSIGNMENT_ID", help="explicitly reuse a retired name")
    crew.add_argument("--verify", metavar="TARGET", help=VERIFY_HELP)
    execution = crew.add_mutually_exclusive_group()
    execution.add_argument(
        "--background",
        action="store_true",
        help="one read-only headless turn with no pane; its report arrives through wait",
    )
    execution.add_argument(
        "--foreground", action="store_true", help="open a pane/tab, overriding profile execution"
    )
    crew.add_argument(
        "--placement",
        "--p",
        choices=(*PLACEMENTS, "auto"),
        help="the placement explicitly chosen by the user",
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
    crew.add_argument("--json", action="store_true", help=JSON_HELP)
    wait = commands.add_parser("wait", help="wait for a crew notification")
    wait.add_argument("name", nargs="?", help="crew name or ID (case-insensitive)")
    wait.add_argument(
        "--all", action="store_true", help="return pending deliveries across the fleet"
    )
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
    tell.add_argument("--assignment", help="the crew's live assignment ID (required)")
    tell.add_argument("--json", action="store_true", help=JSON_HELP)
    inbox = commands.add_parser(
        "inbox", help="print a crew's queued mail oldest first and write the read receipt"
    )
    inbox.add_argument("name", help="crew name or ID (case-insensitive)")
    for verb in ("assign", "ask", "answer", "done", "check"):
        command = commands.add_parser(verb, help=f"{verb} an explicit crew assignment")
        command.add_argument("name")
        if verb == "assign":
            command.add_argument("--task", required=True)
            command.add_argument("--owns", action="append", default=[], metavar="PATH")
            command.add_argument("--allow", action="append", default=[], choices=protocol.ACTIONS)
            command.add_argument("--handoff", required=True, metavar="ASSIGNMENT_ID")
            command.add_argument("--verify", metavar="TARGET", help=VERIFY_HELP)
        else:
            command.add_argument("--assignment", required=verb not in ("ask", "done", "check"))
            command.add_argument("--incarnation", default=os.environ.get("CAPTAIN_INCARNATION"))
        if verb == "ask":
            command.add_argument("question")
        elif verb == "answer":
            command.add_argument("question_id")
            command.add_argument("message")
            command.add_argument("--json", action="store_true", help=JSON_HELP)
        elif verb == "done":
            command.add_argument("--report", required=True)
        elif verb == "check":
            command.add_argument("action", choices=protocol.ACTIONS)
            command.add_argument("path", nargs="*")
    escalate = commands.add_parser(
        "escalate", help="in an app session, route a crew's pending question to the user"
    )
    escalate.add_argument("name", help="crew name or ID (case-insensitive)")
    escalate.add_argument("question_id")
    grant = commands.add_parser(
        "grant", help="let a crew assignment run one exact flagged command once"
    )
    grant.add_argument("name", help="crew name or ID (case-insensitive)")
    grant.add_argument(
        "--command",
        dest="command_text",
        required=True,
        help="the exact command text, matched once",
    )
    grant.add_argument("--assignment", required=True, help="the crew's live assignment ID")
    model = commands.add_parser("model", help="switch a running crew to another model")
    model.add_argument("name", help="crew name or ID (case-insensitive)")
    model.add_argument("model", help=f"tier ({'|'.join(TIER_NAMES)}), model name, or alias")
    model.add_argument("--json", action="store_true", help=JSON_HELP)
    shown = commands.add_parser(
        "settings", help="the merged settings and the file that set each, with no session"
    )
    shown.add_argument("--json", action="store_true", help="the settings as one JSON line")
    models_cmd = commands.add_parser(
        "models", help="list valid model IDs, aliases, and tier resolutions per agent CLI"
    )
    models_cmd.add_argument(
        "--agent", choices=PROVIDERS, help="show only this provider (shows all when omitted)"
    )
    models_cmd.add_argument(
        "--json",
        action="store_true",
        help="JSON catalogue with native capabilities (codex, claude, pi)",
    )
    listed = commands.add_parser(
        "commands", help="slash commands and skills an agent CLI offers here, with no session"
    )
    listed.add_argument("--agent", required=True, choices=("claude", "codex", "pi"))
    listed.add_argument("--cwd", help="list for this project directory (default: this project)")
    listed.add_argument("--json", action="store_true", help="the listing as one JSON line")
    focus = commands.add_parser("focus", help="focus an existing crew's pane and tab")
    focus.add_argument("name", help="crew name or ID (case-insensitive)")
    stop = commands.add_parser(
        "interrupt", help="stop a crew's current turn, keeping its pane and assignment"
    )
    stop.add_argument("name", help="crew name or ID (case-insensitive)")
    stop.add_argument("--reason", help="why, recorded in session memory")
    stop.add_argument("--json", action="store_true", help=JSON_HELP)
    providers = commands.add_parser(
        "providers", help="installed agent CLIs and their sign-in status, with no session"
    )
    providers.add_argument(
        "--agent", choices=PROVIDERS, help="check only this provider (checks all when omitted)"
    )
    providers.add_argument("--json", action="store_true", help="the status rows as one JSON line")
    commands.add_parser("session", help="print the current session id")
    listing = commands.add_parser(
        "sessions", help="JSON list of every project's sessions and their state"
    )
    listing.add_argument(
        "--json", action="store_true", help="add each app session's needs-you and unreviewed counts"
    )
    listing.add_argument("--all", action="store_true", help="include archived sessions")
    end = commands.add_parser(
        "end", help="end a stopped app session with no supervisor running; prints JSON"
    )
    end.add_argument("--session", default=argparse.SUPPRESS, help="end this session")
    for verb, summary in (
        ("archive", "archive a stopped session, hiding it from `sessions`; prints JSON"),
        ("unarchive", "clear a session's archive mark; prints JSON"),
    ):
        commands.add_parser(verb, help=summary).add_argument(
            "--session", default=argparse.SUPPRESS, help=f"{verb} this session"
        )
    delete = commands.add_parser("delete", help="delete a stopped session's state; prints JSON")
    delete.add_argument("--session", default=argparse.SUPPRESS, help="delete this session")
    delete.add_argument(
        "--force", action="store_true", help="discard unread questions, reports, mail and results"
    )
    delete.add_argument(
        "--discard-worktrees",
        action="store_true",
        help="discard and remove stopped crew worktrees through their cleanup path",
    )
    delete.add_argument("--json", action="store_true", help=JSON_HELP)
    commands.add_parser("recover", help="JSON digest of memory, assignments and pending deliveries")
    supervise = commands.add_parser(
        "supervise", help="own this session's crew behind a socket for an app, with no Herdr"
    )
    supervise.add_argument("--session", default=argparse.SUPPRESS, help="supervise this session")
    commands.add_parser("update", help="upgrade the installed captain-barbossa tool")
    start = commands.add_parser(
        "init", help="create .captain/settings.toml without changing an existing file"
    )
    start.add_argument(
        "--global",
        dest="home",
        action="store_true",
        help="write ~/.captain/settings.toml instead of the project's",
    )
    status = commands.add_parser("status", help="print a table of this session's crew")
    status.add_argument("--all", action="store_true", help="include dismissed crew")
    status.add_argument("--json", action="store_true", help=JSON_HELP)
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
    board.add_argument(
        "--once",
        action="store_true",
        help="print one frame and exit, for a token and cost reading with no pane",
    )
    board.add_argument(
        "--json", action="store_true", help="print one unformatted usage snapshot as JSON and exit"
    )
    commands.add_parser("relayout", help="repack auto-placed crew into the declared tab shapes")
    dismiss = commands.add_parser("dismiss", help="close an existing crew's pane and retire it")
    dismiss.add_argument("name", help="crew name or ID (case-insensitive)")
    dismiss.add_argument(
        "--confirm-unlanded",
        action="store_true",
        help="the user agreed to dismiss although Git shows the crew's owned work unlanded",
    )
    dismiss.add_argument(
        "--force",
        action="store_true",
        help="the user said to dismiss without a report; skips report, ack and unlanded checks",
    )
    dismiss.add_argument("--json", action="store_true", help=JSON_HELP)
    mem = commands.add_parser(
        "memory", help="session, project, committed repo, and user preference graph memory"
    )
    actions = mem.add_subparsers(dest="memory_command", required=True)
    add = actions.add_parser("add", help="remember a subject → relation → object")
    add.add_argument("subject")
    add.add_argument("relation")
    add.add_argument("target")
    add.add_argument(
        "--scope",
        choices=("session", "project", "repo", "user"),
        default="session",
        help="repo writes the committed .captain/graph.json: curated team facts only; "
        "user holds preferences every captain launch loads, in any project",
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
        help="replace the repo or user fact already recorded for this subject and relation",
    )
    forget = actions.add_parser("forget", help="drop the user preference for a subject → relation")
    forget.add_argument("subject")
    forget.add_argument("relation")
    forget.add_argument("--scope", choices=("user",), required=True)
    query = actions.add_parser(
        "query", help="literal search of session, project, repo, and user memory"
    )
    query.add_argument("question")
    show = actions.add_parser(
        "show", help="show session, project, repo, and user memory relationships"
    )
    show.add_argument(
        "--scope",
        choices=("session", "project", "repo", "user"),
        help="show one scope instead of all",
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
    path.add_argument("--scope", choices=("session", "project", "repo", "user"), default="session")
    prune = actions.add_parser("prune", help="remove finished sessions' memory directories")
    prune.add_argument(
        "--older-than",
        type=float,
        default=PRUNE_DAYS,
        metavar="DAYS",
        help=f"age in days a session must exceed to be removed (default {PRUNE_DAYS})",
    )
    return root


CREW_VERBS = ("ask", "done", "check", "inbox")


def guard_crew(args):
    """Refuse captain-only commands and hide other scopes when running as crew."""
    if os.environ.get("CAPTAIN_ROLE") != "crew":
        return
    if args.command in CREW_VERBS:
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


def resume_target(args):
    """Choose the session to resume and chdir to its project; None after only listing."""
    current = active_project()
    if args.resume:
        row = find_session(current, args.resume)
    else:
        rows = project_sessions(current)
        if not rows:
            raise CaptainError("No sessions to resume.")
        when = lambda row: time.strftime("%Y-%m-%d %H:%M", time.localtime(row[3]))  # noqa: E731
        labels = {row[2]: f"{row[1] or '-'}  {row[2]}  {when(row)}" for row in rows}
        if not sys.stdin.isatty():
            print(*(labels[row[2]] for row in rows), sep="\n")
            return None
        cards = [(row[2], *session_card(row)) for row in rows]
        chosen = pick_session(current.name, cards)
        if chosen is None:
            raise CaptainError("Resume cancelled.")
        row = next(row for row in rows if row[2] == chosen)
    if not os.path.isdir(row[0]):
        raise CaptainError(f"Project {row[0]} of session {row[2]} is not a directory here.")
    os.chdir(row[0])
    args.session = row[2]
    return Path(row[0])


def print_inbox(args, pane, project):
    """Mail for a crew with no delivery hook, and a manual re-read for one that has.

    Same renderer as the hook, so the two never show the crew different things.
    """
    _, crew = Crew.for_args(args, pane, project)
    body, message_ids = mail_context(crew.events)
    if body is None:
        print("No mail.")
        return
    print(body)
    sys.stdout.flush()
    receipt_for(crew.events, message_ids)


def print_models(args):
    """Valid model IDs, aliases, and tier resolutions, per agent CLI (`pi` is per-install)."""
    for provider in (args.agent,) if args.agent else PROVIDERS:
        print(f"{provider}:")
        try:
            entries = models_for(provider)
            tiers = tiers_for(provider)
        except CaptainError as exc:
            print(f"  {exc}")
            continue
        listed = ", ".join(
            f"{model} ({', '.join(aliases)})" if aliases else model for model, aliases in entries
        )
        print(f"  models: {listed}")
        print(f"  tiers: {' '.join(f'{name}={tiers[name]}' for name in TIER_NAMES)}")


def print_settings(args):
    described = config.described()
    if args.json:
        print(json.dumps(described))
        return
    for names, value in config.leaves(described["settings"]):
        dotted = ".".join(names)
        print(f"{dotted} = {json.dumps(value)}  ({described['sources'][dotted]})")


def print_commands(args):
    """One agent CLI's slash commands and skills for a project; needs no session or supervisor."""
    # Local so no other command pays for the native probes.
    from .command_catalogue import catalogue

    root = Path(args.cwd).expanduser().resolve() if args.cwd else active_project()
    if not root.is_dir():
        raise CaptainError(f"[commands-cwd] {root} is not a directory.")
    listing = catalogue(root, args.agent)
    if args.json:
        print(json.dumps(listing))
        return
    for row in listing["commands"]:
        usage = " ".join(part for part in (f"/{row['name']}", row["argument_hint"]) if part)
        print(f"{usage}  {row['description'] or ''}".rstrip())
    for row in listing["skills"]:
        print(f"skill {row['name']}  {row['description'] or ''}".rstrip())


def print_providers(args):
    """Install and sign-in status per agent CLI; needs no session, Herdr or supervisor."""
    # Local: the probes load the supervised adapters, which no other command needs.
    from .provider_status import catalogue

    status = catalogue(active_project(), args.agent)
    if args.json:
        print(json.dumps(status))
        return
    signed = {True: "signed in", False: "signed out", None: "sign-in unknown"}
    for row in status["providers"]:
        if row["installed"] is None:
            print(f"{row['provider']}: {row['detail']}")
            continue
        if not row["installed"]:
            print(f"{row['provider']}: not installed")
            continue
        version = row["version"] or "version unknown"
        print(f"{row['provider']}: {version}, {signed[row['signed_in']]}")


def pump_mail(args, pane, project):
    """Retry held doorbells for every live crew, so captain activity drains mail outside a wait.

    Never fails the captain's command, and crew never ring each other.
    """
    if os.environ.get("CAPTAIN_ROLE") == "crew":
        return
    try:
        crews = Crew.members(read_session(project, args.session, pane))
    except Exception:
        return
    for crew in crews:
        if crew.is_dismissed:
            continue
        try:
            protocol.drain(crew)
        except Exception:
            pass


def paneless_target(args):
    """Whether this wait or interrupt reaches only background crew of an explicit session.

    Those need the session alone, so no Herdr pane is resolved, even from a stale Herdr
    environment. Anything involving a visual crew still needs Herdr: its wait rings
    doorbells, and ringing without Herdr would bounce its mail. Anything unresolvable
    falls through to the usual pane lookup and its error. Only a caller holding the
    supervisor token reads the session to find a supervised one, where nothing has a pane.
    """
    if os.environ.get(SUPERVISOR_TOKEN_ENV) and args.session:
        try:
            if read_session(active_project(), args.session).meta.get("transport") == "supervisor":
                return True
        except CaptainError:
            pass  # not a readable supervised session: the checks below decide as before
    if args.command not in ("wait", "interrupt") or not args.session:
        return False
    try:
        current = read_session(active_project(), args.session)
        if args.name:
            crews = [Crew.resolve(current, args.name)]
        else:
            crews = [crew for crew in Crew.members(current) if not crew.is_dismissed]
    except CaptainError:
        return False
    return bool(crews) and all(crew.is_background for crew in crews)


def is_launch_plan(argv):
    """Inspect only the subcommand, skipping root options and their values."""
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        index += 1
        if "=" in option:
            continue
        if any(
            name.startswith(option) for name in ("--agent", "--session", "--prompt", "--rename")
        ):
            index += 1
        # Like argparse, --resume takes the next word as its target unless it is an option.
        elif (
            "--resume".startswith(option) and index < len(argv) and not argv[index].startswith("-")
        ):
            index += 1
    return argv[index : index + 1] == ["launch-plan"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        # Native hooks fire per event: skip the parser, update check and Herdr pane lookup.
        events.append_event(argv[1:])
        return 0
    is_plan = is_launch_plan(argv)
    try:
        args = parser(plan=is_plan).parse_args(argv)
    except LaunchPlanError as exc:
        print(json.dumps({"version": 1, "error": {"code": exc.code, "message": str(exc)}}))
        return 1
    try:
        guard_crew(args)
        # LaunchPlanError so launch-plan reports these as its JSON error too.
        if args.if_unnamed and args.rename is None:
            raise LaunchPlanError("invalid-arguments", "--if-unnamed needs --rename.")
        if args.command and (args.rename is not None or args.resume is not None):
            # guard_crew passes crew subcommands, so a flag riding one must not run.
            raise LaunchPlanError("invalid-arguments", "--rename and --resume take no subcommand.")
        if args.command == "launch-plan":
            print(json.dumps(launch_plan(args, active_project())))
            return 0
        if args.rename is not None:
            directory = read_session(active_project(), args.session).directory
            rename_session(directory, args.rename, if_unnamed=args.if_unnamed)
            return 0
        if args.resume is not None:
            project = resume_target(args)
            if project is None:
                return 0
            launch(args, current_pane(), project)
            return 0
        if args.command is None:
            bootstrap(args)
        if args.command == "init":
            config.init_settings(args)
            return 0
        if args.command == "session":
            print_session(args)
            return 0
        if args.command == "sessions" and args.json:
            from .supervisor import counted_catalog  # local, as for supervise

            counted = {"include_archived": True} if args.all else {}
            print(json.dumps(counted_catalog(active_project(), **counted)))
            return 0
        if args.command == "sessions":
            print(json.dumps(catalog(active_project(), include_archived=args.all)))
            return 0
        if args.command in ARCHIVE_VERBS:
            directory = read_session(active_project(), args.session).directory
            options = (
                {
                    "force": args.force,
                    "discard_worktrees": args.discard_worktrees,
                    "worktree_cleanup": cleanup_worktree,
                }
                if args.command == "delete"
                else {}
            )
            print(json.dumps(ARCHIVE_VERBS[args.command](directory, **options)))
            return 0
        if args.command == "end":
            from .supervisor import end_session  # local, as for supervise

            directory = read_session(active_project(), args.session).directory
            print(json.dumps(end_session(directory)))
            return 0
        if args.command in ("result", "worktree"):
            print(json.dumps(artifact_command(args, read_session(active_project(), args.session))))
            return 0
        if args.command == "recover":
            project = active_project()
            current = read_session(project, args.session)
            digest = recovery.digest(current, project)
            if current.meta.get("plugins"):
                from .plugins import context

                digest["plugins"] = context(current.meta["plugins"], "captain")
            print(json.dumps(digest))
            return 0
        if args.command == "plugin":
            from .plugins import command

            return command(args)
        if args.command == "update":
            return upgrade()
        if args.command == "inspect":
            inspection.run(args, active_project())
            return 0
        if args.command == "supervise":
            # Local so a Herdr command never loads the supervisor and its adapters.
            from .supervisor import run

            return run(args, active_project())
        if args.command == "status":
            status_crew(args, None, active_project())
            return 0
        if args.command == "memory" and args.memory_command in ("show", "path"):
            memory(args, None, active_project())
            return 0
        if args.command == "providers":
            print_providers(args)
            return 0
        if args.command == "settings":
            active_project()
            print_settings(args)
            return 0
        if args.command == "commands":
            print_commands(args)
            return 0
        if args.command == "models":
            if args.json:
                from .model_catalogue import catalogue

                print(json.dumps(catalogue(active_project(), args.agent)))
            else:
                print_models(args)
            return 0
        if args.command == "memory" and args.memory_command == "prune":
            config.in_range(args.older_than, "--older-than", below=float("inf"))
        # Crew protocol verbs resolve identity from CAPTAIN_* env, so any cwd works, and need
        # no pane: a sandboxed crew cannot reach the Herdr socket at all.
        as_crew = os.environ.get("CAPTAIN_ROLE") == "crew"
        paneless = as_crew and args.command in (*CREW_VERBS, "memory")
        pane = None if paneless or paneless_target(args) else current_pane()
        project = project_root() if as_crew and args.command in CREW_VERBS else active_project()
        if pane is not None:
            pump_mail(args, pane, project)
        if args.command == "crew":
            create_crew(args, pane, project)
        elif args.command == "wait":
            wait_crew(args, pane, project)
        elif args.command == "tell":
            tell_crew(args, pane, project)
        elif args.command == "inbox":
            print_inbox(args, pane, project)
        elif args.command in ("assign", "ask", "answer", "done", "check"):
            protocol_command(args, pane, project)
        elif args.command == "grant":
            _, member = Crew.for_args(args, pane, project)
            print(json.dumps(protocol.grant(member, args.assignment, args.command_text)))
        elif args.command == "escalate":
            _, member = Crew.for_args(args, pane, project)
            print(json.dumps(protocol.escalate(member, args.question_id)))
        elif args.command == "model":
            switch_model(args, pane, project)
        elif args.command == "focus":
            focus_crew(args, pane, project)
        elif args.command == "interrupt":
            interrupt_crew(args, pane, project)
        elif args.command in ("do", "quiet"):
            current = read_session(project, args.session, pane)
            do.run(args, current.directory, project, current.meta)
        elif args.command == "dashboard":
            if args.json:
                print(json.dumps(dashboard.snapshot(read_session(project, args.session, pane))))
            elif args.once:
                print(dashboard.render(read_session(project, args.session, pane)))
            else:
                dashboard.run_dashboard(args, pane, project)
        elif args.command == "dismiss":
            dismiss_crew(args, pane, project)
        elif args.command == "relayout":
            relayout_crew(args, pane, project)
        elif args.command == "memory":
            memory(args, pane, project)
        else:
            launch(args, pane, project)
    except (*HERDR_ERRORS, EOFError) as exc:
        if args.command == "launch-plan":
            print(
                json.dumps(
                    {
                        "version": 1,
                        "error": {
                            "code": getattr(exc, "code", "launch-failed"),
                            "message": str(exc),
                        },
                    }
                )
            )
            return 1
        if args.command == "wait" and args.json:
            if args.all:
                print(json.dumps({"status": "error", "deliveries": [], "summary": str(exc)}))
                return 1
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
        if args.command == "delete":
            blockers = getattr(exc, "blockers", [])
            if args.json:
                match = REFUSAL_CODE.match(str(exc))
                code = match[1] if match else "captain-error"
                print(
                    json.dumps(
                        {
                            "error": {
                                "code": code,
                                "message": str(exc),
                                "data": {"blockers": blockers},
                            }
                        }
                    )
                )
            else:
                print(f"captain: {exc}", file=sys.stderr)
                for blocker in blockers:
                    print(
                        f"captain: blocker ({blocker['kind']}): {blocker['summary']}",
                        file=sys.stderr,
                    )
            return 1
        if args.command in JSON_VERBS and args.json:
            match = REFUSAL_CODE.match(str(exc))
            code = match[1] if match else "captain-error"
            print(json.dumps({"error": {"code": code, "message": str(exc)}}))
            return 1
        print(f"captain: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
