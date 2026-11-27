"""Session-independent native provider installation and sign-in checks."""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import (
    __version__,
    claude_agent,
    codex_app_server,
    config,
    eventlog,
    instructions,
    jsonrpc,
    model_catalogue,
    pi_rpc,
    processes,
    runtime,
)
from .models import CAPABILITIES, PROVIDERS
from .runtime import CaptainError

# Any crew-named events path: the hook command Codex trusts drops it, so one probe covers all.
TRUST_PROBE = Path("hook-trust.jsonl")
HOOK_CODE = re.compile(r"\[codex-hooks-(missing|untrusted)\] ")

ACCOUNT_PROBES = {
    "codex": codex_app_server.account,
    "claude": claude_agent.account,
    "pi": pi_rpc.account,
}

CLAUDE_CHOICES = re.compile(r"--permission-mode\b.*?\(choices:\s*([^)]*)\)", re.S)
# Claude's CLI spells the SDK's "default" as "manual".
CLAUDE_ALIASES = {"manual": "default"}
# (name, description) per SDK mode id, in display order; claude exposes ids but no text.
CLAUDE_MODES = {
    "default": ("Default", "Asks before edits and commands."),
    "acceptEdits": ("Accept edits", "Edits files without asking; asks before commands."),
    "plan": (
        "Plan",
        "Reads and plans only, then asks to approve the plan before changing anything.",
    ),
    "auto": ("Auto", "A classifier approves safe actions and asks about risky ones."),
    "dontAsk": ("Don't ask", "Runs only pre-approved tools and denies everything else."),
    "bypassPermissions": ("Bypass permissions", "Runs every tool without asking."),
}
# Codex presets as the native thread/start and turn/start fields; the app-server lists none.
CODEX_PRESETS = {
    "read-only": {
        "name": "Read-only",
        "description": "Reads files; asks before editing or running commands.",
        "approval": "on-request",
        "sandbox": "read-only",
        "reviewer": "user",
    },
    "auto": {
        "name": "Auto",
        "description": "Edits and runs commands in the workspace; asks to go beyond it.",
        "approval": "on-request",
        "sandbox": "workspace-write",
        "reviewer": "user",
    },
    "auto-review": {
        "name": "Auto review",
        "description": "Workspace sandbox; a Codex reviewer answers approval requests.",
        "approval": "on-request",
        "sandbox": "workspace-write",
        "reviewer": "auto_review",
    },
    "full-access": {
        "name": "Full access",
        "description": "No sandbox and no approval prompts.",
        "approval": "never",
        "sandbox": "danger-full-access",
        "reviewer": "user",
    },
}
# What Codex selects when config.toml sets none of them.
CODEX_UNSET = {"approval": "on-request", "sandbox": "workspace-write", "reviewer": "user"}
# The sandbox a built-in `default_permissions` profile selects when sandbox_mode is unset.
CODEX_PROFILE_SANDBOX = {
    ":read-only": "read-only",
    ":workspace": "workspace-write",
    ":danger-full-access": "danger-full-access",
}


def probe(provider: str, root: str | Path) -> dict:
    """Read native status without creating a Captain session; unavailable facts stay null."""
    timeout = config.number("supervisor", "startup_timeout")
    row = {
        "provider": provider,
        "installed": False,
        "version": None,
        "signed_in": None,
        "account": None,
        "detail": None,
        "hooks": None,
        "permission_modes": None,
        "checked_at": time.time(),
    }
    try:
        binary = runtime.executable(provider)
    except CaptainError as exc:
        row["detail"] = str(exc)
        return row
    row["installed"] = True
    with contextlib.suppress(CaptainError, OSError):
        result = processes.capture(root, [binary, "--version"], timeout)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        row["version"] = lines[0] if result.returncode == 0 and lines else None
    account = ACCOUNT_PROBES.get(provider)
    if account is not None:
        try:
            row.update(account(binary, root, timeout))
        except (CaptainError, OSError) as exc:
            row["detail"] = str(exc)
    if provider == "codex":
        row["hooks"] = codex_hooks(binary, root, timeout)
    row["permission_modes"] = permission_modes(provider, binary, root, timeout)
    return row


def mode_row(identifier, name, description, default):
    return {"id": identifier, "name": name, "description": description, "default": default}


def permission_modes(provider, binary, root, timeout):
    """The modes a provider's captain can start in, read without a model turn; pi has none."""
    match provider:
        case "claude":
            return claude_modes(binary, root, timeout)
        case "codex":
            return codex_modes(binary, root, timeout)
    return []


def claude_modes(binary, root, timeout):
    """Claude's `--permission-mode` choices; `default` marks the user's settings defaultMode."""
    chosen = None
    with contextlib.suppress(CaptainError, OSError):
        result = processes.capture(root, [binary, "--help"], timeout)
        found = CLAUDE_CHOICES.search(result.stdout)
        if result.returncode == 0 and found:
            names = [name.strip(" \n\"'") for name in found[1].split(",")]
            chosen = [CLAUDE_ALIASES.get(name, name) for name in names if name]
    ids = [*CLAUDE_MODES] if chosen is None else [m for m in CLAUDE_MODES if m in chosen]
    ids += [name for name in chosen or () if name not in CLAUDE_MODES]
    selected = claude_default_mode(root)
    return [mode_row(name, *CLAUDE_MODES.get(name, (name, "")), name == selected) for name in ids]


def claude_default_mode(root):
    """permissions.defaultMode from claude's local, project, then user settings; else `default`."""
    home = Path.home() / ".claude"
    project = Path(root) / ".claude"
    for path in (
        project / "settings.local.json",
        project / "settings.json",
        home / "settings.json",
    ):
        try:
            permissions = json.loads(path.read_text()).get("permissions")
        except (OSError, ValueError, AttributeError):
            continue
        mode = permissions.get("defaultMode") if isinstance(permissions, dict) else None
        if isinstance(mode, str) and mode:
            return CLAUDE_ALIASES.get(mode, mode)
    return "default"


def codex_modes(binary, root, timeout):
    """Codex presets its requirements allow; `default` marks what `config/read` selects."""
    try:
        with model_catalogue.native_peer([binary, "app-server"], Path(root), jsonrpc.Peer) as peer:
            client = {"name": "captain-barbossa", "version": __version__}
            peer.request("initialize", {"clientInfo": client}, timeout)
            peer.notify("initialized", timeout)
            config_read = peer.request("config/read", {"cwd": str(root)}, timeout)
            required = peer.request("configRequirements/read", {}, timeout)
    except (CaptainError, OSError):
        return codex_rows({}, {})
    return codex_rows(config_read, required)


def codex_rows(config_read, required):
    """Presets filtered by allowed policies and sandboxes; no default when the config is unread."""
    settings = config_read.get("config") if isinstance(config_read, dict) else None
    limits = required.get("requirements") if isinstance(required, dict) else None
    limits = limits if isinstance(limits, dict) else {}
    selected = None
    if isinstance(settings, dict):
        profile = CODEX_PROFILE_SANDBOX.get(str(settings.get("default_permissions")))
        wanted = {
            key: settings.get(native) or fallback
            for key, native, fallback in (
                ("approval", "approval_policy", CODEX_UNSET["approval"]),
                ("sandbox", "sandbox_mode", profile or CODEX_UNSET["sandbox"]),
                ("reviewer", "approvals_reviewer", CODEX_UNSET["reviewer"]),
            )
        }
        if wanted["reviewer"] == "guardian_subagent":
            wanted["reviewer"] = "auto_review"
        selected = next(
            (
                name
                for name, preset in CODEX_PRESETS.items()
                if all(preset[key] == wanted[key] for key in CODEX_UNSET)
            ),
            None,
        )
    rows = []
    for name, preset in CODEX_PRESETS.items():
        approvals = limits.get("allowedApprovalPolicies")
        sandboxes = limits.get("allowedSandboxModes")
        if isinstance(approvals, list) and preset["approval"] not in approvals:
            continue
        if isinstance(sandboxes, list) and preset["sandbox"] not in sandboxes:
            continue
        rows.append(mode_row(name, preset["name"], preset["description"], name == selected))
    return rows


def codex_hooks(binary, root, timeout):
    """Codex's trust of captain's crew hooks, read with `hooks/list`; trust is never written."""
    flags = instructions.native_args("codex", "", events=TRUST_PROBE, transport="app-server")
    try:
        with model_catalogue.native_peer(
            [binary, "app-server", *flags], Path(root), jsonrpc.Peer
        ) as peer:
            client = {"name": "captain-barbossa", "version": __version__}
            peer.request("initialize", {"clientInfo": client}, timeout)
            peer.notify("initialized", timeout)
            listed = peer.request("hooks/list", {"cwds": [str(root)]}, timeout)
        codex_app_server.require_live_hooks(SimpleNamespace(events=TRUST_PROBE), listed)
    except (CaptainError, OSError) as exc:
        match = HOOK_CODE.match(str(exc))
        return {"state": match[1] if match else "unknown", "detail": str(exc)}
    return {"state": "trusted", "detail": None}


def catalogue(root: str | Path, provider: str | None = None) -> dict:
    """Bound concurrent probes by one deadline and retain a typed row for every provider."""
    if provider is not None and provider not in PROVIDERS:
        raise CaptainError(f"[provider-check] provider must be one of {', '.join(PROVIDERS)}.")
    names = [provider] if provider else list(PROVIDERS)
    found = {}
    threads = [
        threading.Thread(
            target=lambda name: found.update({name: probe(name, root)}), args=(name,), daemon=True
        )
        for name in names
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2 * config.number("supervisor", "startup_timeout")
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    rows = []
    for name in names:
        rows.append(
            {
                "provider": name,
                "installed": None,
                "version": None,
                "signed_in": None,
                "account": None,
                "hooks": None,
                "permission_modes": None,
                "checked_at": None,
                "detail": "[provider-check] The check did not finish before its deadline.",
                **found.get(name, {}),
                "sign_in": CAPABILITIES[name]["sign_in"],
                "attempt": None,
            }
        )
    return eventlog.redact({"version": 1, "providers": rows}, eventlog.known_secrets())
