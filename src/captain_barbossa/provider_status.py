"""Session-independent native provider installation and sign-in checks."""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path

from . import claude_agent, codex_app_server, config, eventlog, pi_rpc, processes, runtime
from .models import CAPABILITIES, PROVIDERS
from .runtime import CaptainError

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
    return row


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
                "checked_at": None,
                "detail": "[provider-check] The check did not finish before its deadline.",
                **found.get(name, {}),
                "sign_in": CAPABILITIES[name]["sign_in"],
                "attempt": None,
            }
        )
    return eventlog.redact({"version": 1, "providers": rows}, eventlog.known_secrets())
