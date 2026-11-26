"""Slash commands and skills of a native tool, in one shape for the running captain and the CLI."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import __version__, instructions, model_catalogue, processes, runtime
from .runtime import CaptainError

PROVIDERS = ("claude", "codex", "pi")
FRONTMATTER_BYTES = 64 * 1024
# YAML block markers: the description is on the lines after them, so the native's own text wins.
BLOCK_SCALARS = frozenset((">", "|", ">-", "|-", ">+", "|+"))


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def command(name, description=None, argument_hint=None):
    return {"name": name, "description": _text(description), "argument_hint": _text(argument_hint)}


def normalize(listing):
    """{commands: [{name, description, argument_hint}], skills: [{name, description}]} of any listing."""
    listing = listing if isinstance(listing, dict) else {}
    rows = {
        kind: listing.get(kind) if isinstance(listing.get(kind), list) else []
        for kind in ("commands", "skills")
    }
    return {
        "commands": [
            command(row["name"], row.get("description"), row.get("argument_hint"))
            for row in rows["commands"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        ],
        "skills": [
            {"name": row["name"], "description": _text(row.get("description"))}
            for row in rows["skills"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        ],
    }


def names(listing):
    return {row["name"] for kind in ("commands", "skills") for row in listing[kind]}


def frontmatter(path):
    """Top-level `key: value` pairs of a markdown file's leading frontmatter block."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as file:
            lines = file.read(FRONTMATTER_BYTES).splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    found = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return found
        key, separator, value = line.partition(":")
        if separator and key.strip() and not line[:1].isspace() and not key.startswith("#"):
            found[key.strip()] = value.strip().strip("\"'")
    return {}


def _claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _claude_dirs(root, kind):
    """(name prefix, directory) of each place claude loads `kind` (skills or commands) from."""
    home = _claude_home()
    yield "", home / kind
    for directory in (root, *root.parents):
        if directory == Path.home():
            break
        yield "", directory / ".claude" / kind
    try:
        installed = json.loads((home / "plugins" / "installed_plugins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return
    plugins = installed.get("plugins") if isinstance(installed, dict) else None
    for key, installs in plugins.items() if isinstance(plugins, dict) else ():
        for install in installs if isinstance(installs, list) else ():
            path = install.get("installPath") if isinstance(install, dict) else None
            if isinstance(key, str) and isinstance(path, str):
                yield f"{key.partition('@')[0]}:", Path(path) / kind


def claude_skills(root):
    """Skill name -> its SKILL.md frontmatter, from the user's, project and plugin skill dirs."""
    found = {}
    for prefix, directory in _claude_dirs(root, "skills"):
        for path in sorted(directory.glob("*/SKILL.md")) if directory.is_dir() else ():
            meta = frontmatter(path)
            found.setdefault(prefix + (_text(meta.get("name")) or path.parent.name), meta)
    return found


def claude_command_files(root):
    found = set()
    for prefix, directory in _claude_dirs(root, "commands"):
        for path in directory.glob("*.md") if directory.is_dir() else ():
            found.add(prefix + path.stem)
    return found


def claude_listing(server, root):
    """Claude's get_server_info commands split by origin: built-ins and commands/*.md are commands,
    the rest skills; a skill whose frontmatter says user-invocable: false is left out."""
    skills, files = claude_skills(Path(root)), claude_command_files(Path(root))
    listing = {"commands": [], "skills": []}
    for row in server if isinstance(server, list) else ():
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name:
            continue
        meta = skills.get(name, {})
        if meta.get("user-invocable", "").lower() == "false":
            continue
        if row.get("builtin") is True or (name in files and name not in skills):
            listing["commands"].append(
                command(name, row.get("description"), row.get("argumentHint"))
            )
            continue
        described = meta.get("description")
        described = None if described in BLOCK_SCALARS else _text(described)
        listing["skills"].append(
            {"name": name, "description": described or _text(row.get("description"))}
        )
    return listing


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaptainError("Command discovery exceeded its deadline.")
    return remaining


def claude_commands(root):
    argv = [instructions.python_command(), "-m", "captain_barbossa.claude_host", "--commands"]
    result = processes.capture(
        root, [*argv, runtime.executable("claude")], model_catalogue.PROBE_TIMEOUT
    )
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        reply = {}
    if not isinstance(reply, dict) or "commands" not in reply:
        error = reply.get("error") if isinstance(reply, dict) else None
        detail = error or (result.stderr or "").strip()[-500:] or "no command list"
        raise CaptainError(detail)
    return claude_listing(reply["commands"], root)


def codex_commands(root):
    """The running Codex captain's listing, from a short-lived app-server with no thread."""
    from .codex_app_server import listing
    from .jsonrpc import Peer

    deadline = time.monotonic() + model_catalogue.PROBE_TIMEOUT
    argv = [runtime.executable("codex"), "app-server"]
    with model_catalogue.native_peer(argv, root, Peer) as peer:
        client = {"clientInfo": {"name": "captain-barbossa", "version": __version__}}
        peer.request("initialize", client, _remaining(deadline))
        peer.notify("initialized", _remaining(deadline))
        listed = peer.request("skills/list", {"cwds": [str(root)]}, _remaining(deadline))
    return listing(listed)


def pi_commands(root):
    """The running pi captain's listing, from a sessionless pi without MCP or tools."""
    from .pi_rpc import Peer, listing

    argv = [runtime.executable("pi"), "--mode", "rpc", "--no-session", "--no-mcp", "--no-tools"]
    with model_catalogue.native_peer(argv, root, Peer) as peer:
        listed = peer.request("get_commands", {}, model_catalogue.PROBE_TIMEOUT)
    return listing(listed)


DISCOVER = {"claude": claude_commands, "codex": codex_commands, "pi": pi_commands}


def catalogue(root, agent):
    """One provider's commands and skills for a project, from short-lived native processes."""
    if agent not in PROVIDERS:
        raise CaptainError(
            f"[command-discovery] Command discovery supports {', '.join(PROVIDERS)} only."
        )
    try:
        return normalize(DISCOVER[agent](Path(root)))
    except (CaptainError, OSError) as exc:
        if str(exc).startswith("["):
            raise
        raise CaptainError(f"[command-discovery] {agent}: {exc}") from exc
