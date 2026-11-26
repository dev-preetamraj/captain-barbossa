"""Session-independent native provider installation and sign-in checks."""

from __future__ import annotations

import contextlib
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
    return row


def codex_hooks(binary, root, timeout):
    """Codex's trust of captain's crew hooks, read with `hooks/list`; trust is never written."""
    flags = instructions.native_args("codex", "", events=TRUST_PROBE, transport="app-server")
    try:
        with model_catalogue._native(
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
                "checked_at": None,
                "detail": "[provider-check] The check did not finish before its deadline.",
                **found.get(name, {}),
                "sign_in": CAPABILITIES[name]["sign_in"],
                "attempt": None,
            }
        )
    return eventlog.redact({"version": 1, "providers": rows}, eventlog.known_secrets())
