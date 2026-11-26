"""Session-less model metadata from the installed native tools."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol, TypeVar

from . import __version__, models, processes, runtime, usage
from .runtime import CaptainError

PROVIDERS = ("codex", "claude", "pi")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROBE_TIMEOUT = 15.0
MODEL_VERSION = re.compile(r"(?<!\d)(\d+(?:\.\d+)*)(?!\d)")
FAMILY_WORD = re.compile(r"[a-z]+|\d+")
FAMILY_NOISE = frozenset(("latest", "preview"))


class Peer(Protocol):
    def start(self) -> None: ...
    def request(self, method: str, params: dict, timeout: float) -> object: ...
    def close(self) -> None: ...


NativePeer = TypeVar("NativePeer", bound=Peer)


@contextlib.contextmanager
def native_peer(
    argv: list[str], root: Path, factory: Callable[..., NativePeer]
) -> Iterator[NativePeer]:
    """Own the probe's pipes and process group, including failed startup and timed-out requests."""
    native_in, write_fd = os.pipe()
    read_fd, native_out = os.pipe()
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            stdin=native_in,
            stdout=native_out,
            stderr=subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in processes.CHILD_SECRETS
            },
            start_new_session=True,
        )
    except OSError as exc:
        os.close(write_fd)
        os.close(read_fd)
        raise CaptainError(f"Native probe could not start {argv[0]}: {exc}") from exc
    finally:
        os.close(native_in)
        os.close(native_out)
    peer = None
    started = False
    try:
        peer = factory(read_fd, write_fd, lambda message: None, "model-catalogue")
        peer.start()
        started = True
        yield peer
    finally:
        try:
            if peer is None:
                os.close(write_fd)
            else:
                peer.close()
            if not started:
                os.close(read_fd)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(processes.GROUP_TERMINATE_TIMEOUT)
        finally:
            processes.terminate_process_group(process)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaptainError("Model discovery exceeded its deadline.")
    return remaining


def _rows(value: object, key: str) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise CaptainError(f"Model discovery returned no {key} array.")
    rows = value[key]
    if not all(isinstance(row, dict) for row in rows):
        raise CaptainError(f"Model discovery returned invalid {key} entries.")
    return rows


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CaptainError("Model discovery returned an invalid model ID.")
    return value


def _levels(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(level, str) and level for level in value):
        return None
    return list(dict.fromkeys(value))


def _window(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _name(value: object, identifier: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else identifier


def _model(identifier: str, aliases: tuple[str, ...] = (), name: object = None) -> dict:
    return {
        "id": identifier,
        "name": _name(name, identifier),
        "aliases": list(aliases),
        "context_window": None,
        "effort_levels": None,
    }


def _family_version(row: dict, provider: str) -> tuple[str, tuple[int, ...]] | None:
    source = row["name"]
    scope = ""
    if provider == "pi" and "/" in row["id"]:
        scope, _, model_id = row["id"].partition("/")
        if source == row["id"]:
            source = model_id
    match = MODEL_VERSION.search(source)
    if match is None:
        return None
    words = FAMILY_WORD.findall(f"{source[: match.start()]} {source[match.end() :]}".lower())
    family = " ".join(
        word
        for word in words
        if word not in FAMILY_NOISE and not (len(word) == 8 and word.isdigit())
    )
    if not family:
        return None
    return f"{scope}/{family}" if scope else family, tuple(
        int(part) for part in match.group(1).split(".")
    )


def _mark_legacy(rows: list[dict], provider: str) -> None:
    versions = [(row, _family_version(row, provider)) for row in rows]
    latest: dict[str, tuple[int, ...]] = {}
    for _, parsed in versions:
        if parsed is not None:
            family, version = parsed
            latest[family] = max(latest.get(family, version), version)
    for row, parsed in versions:
        forced_legacy = row.pop("_legacy", False)
        forced_current = row.pop("_current", False)
        if forced_legacy or (
            not forced_current and parsed is not None and parsed[1] < latest[parsed[0]]
        ):
            row["legacy"] = True
        else:
            row.pop("legacy", None)


def _claude_name(identifier: str) -> str:
    match = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d+)*)", identifier)
    if match is None:
        return identifier
    return f"Claude {match.group(1).title()} {match.group(2).replace('-', '.')}"


def _exclude_codex(native: dict) -> bool:
    if native.get("hidden") is True:
        return True
    kind = native.get("type") or native.get("kind")
    if isinstance(kind, str) and kind.lower() not in {"chat", "model"}:
        return True
    modalities = native.get("inputModalities")
    return isinstance(modalities, list) and "text" not in modalities


def codex_models(root: Path) -> list[dict]:
    from .jsonrpc import Peer

    deadline = time.monotonic() + PROBE_TIMEOUT
    with native_peer([runtime.executable("codex"), "app-server"], root, Peer) as peer:
        peer.request(
            "initialize",
            {"clientInfo": {"name": "captain-barbossa", "version": __version__}},
            _remaining(deadline),
        )
        peer.notify("initialized", _remaining(deadline))
        rows, cursors, cursor = [], set(), None
        while True:
            result = peer.request(
                "model/list", {"includeHidden": True, "cursor": cursor}, _remaining(deadline)
            )
            for native in _rows(result, "data"):
                identifier = _identifier(native.get("model") or native.get("id"))
                if _exclude_codex(native):
                    rows.append({"id": identifier, "_exclude": True})
                    continue
                row = _model(identifier, name=native.get("displayName"))
                row["_legacy"] = isinstance(native.get("upgrade"), str) and bool(native["upgrade"])
                row["_current"] = native.get("isDefault") is True
                efforts = native.get("supportedReasoningEfforts")
                if isinstance(efforts, list):
                    row["effort_levels"] = _levels(
                        [
                            effort.get("reasoningEffort") if isinstance(effort, dict) else None
                            for effort in efforts
                        ]
                    )
                row["context_window"] = _window(native.get("contextWindow"))
                rows.append(row)
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise CaptainError("Model discovery returned an invalid or repeated cursor.")
            cursors.add(cursor)


def pi_models(root: Path) -> list[dict]:
    from .pi_rpc import Peer

    argv = [
        runtime.executable("pi"),
        "--mode",
        "rpc",
        "--no-session",
        "--offline",
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--no-tools",
    ]
    deadline = time.monotonic() + PROBE_TIMEOUT
    with native_peer(argv, root, Peer) as peer:
        result = peer.request("get_available_models", {}, _remaining(deadline))
        native_rows = _rows(result, "models")
        counts = Counter(_identifier(row.get("id")) for row in native_rows)
        rows = []
        for native in native_rows:
            model_id, provider = _identifier(native.get("id")), _identifier(native.get("provider"))
            row = _model(
                f"{provider}/{model_id}",
                (model_id,) if counts[model_id] == 1 else (),
                native.get("name"),
            )
            row["context_window"] = _window(native.get("contextWindow"))
            rows.append(row)
        for row, native in zip(rows, native_rows, strict=True):
            try:
                peer.request(
                    "set_model",
                    {"provider": native["provider"], "modelId": native["id"]},
                    _remaining(deadline),
                )
                result = peer.request("get_available_thinking_levels", {}, _remaining(deadline))
            except CaptainError:
                continue
            row["effort_levels"] = (
                _levels(result.get("levels")) if isinstance(result, dict) else None
            )
        return rows


def claude_models(root: Path) -> list[dict]:
    return [
        {
            **_model(identifier, aliases, _claude_name(identifier)),
            "context_window": usage.CONTEXT_WINDOWS.get(identifier),
            "effort_levels": list(CLAUDE_EFFORTS),
        }
        for identifier, aliases in models.MODELS["claude"]
    ]


DISCOVER = {"codex": codex_models, "claude": claude_models, "pi": pi_models}


def catalogue(root: Path, agent: str | None = None) -> dict:
    """Return known models even when native metadata is unavailable, leaving unknown fields null."""
    if agent is not None and agent not in PROVIDERS:
        raise CaptainError("JSON model discovery supports codex, claude and pi only.")
    providers = []
    for provider in (agent,) if agent else PROVIDERS:
        rows = {
            identifier: _model(identifier, aliases)
            for identifier, aliases in models.MODELS.get(provider, ())
        }
        error = None
        try:
            discovered = DISCOVER[provider](root)
        except (CaptainError, OSError) as exc:
            error = str(exc)
        else:
            for row in discovered:
                if row.pop("_exclude", False):
                    rows.pop(row["id"], None)
                    continue
                if row["id"] in rows:
                    row["aliases"] = rows[row["id"]]["aliases"]
                rows[row["id"]] = row
        _mark_legacy(list(rows.values()), provider)
        entries = tuple((row["id"], tuple(row["aliases"])) for row in rows.values())
        tiers = models.tier_mapping(provider, entries)
        for identifier in tiers.values():
            rows.setdefault(identifier, _model(identifier))
        providers.append(
            {
                "provider": provider,
                "tiers": {tier: tiers.get(tier) for tier in models.TIER_NAMES},
                "models": list(rows.values()),
                "error": error,
            }
        )
    return {"version": 1, "providers": providers}
