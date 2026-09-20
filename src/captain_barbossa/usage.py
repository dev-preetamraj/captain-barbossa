"""Token usage and cost for a crew member, read from the session file its native CLI writes."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config, memory, models

# Prices change without a new model shipping, so they are fetched rather than tabulated
# here. LiteLLM carries every id models.MODELS launches.
PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
PRICES_TTL = 24 * 3600
RATE_WINDOW = 600  # seconds of trailing turns behind the $/h reading
# Codex rollout logs, one per thread, named rollout-<stamp>-<thread id>.jsonl.
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

# Bundled fallback for the Claude context windows, used only when the price cache is
# cold; LiteLLM's max_input_tokens wins whenever it is warm. A window is a stable fact
# that moves only when a model ships, so a fetch must never decide whether CTX renders.
# Source: Anthropic "Models overview",
# https://platform.claude.com/docs/en/about-claude/models/overview, "Context window" row,
# read 2026-09-20. Codex needs no entry; its rollout log states model_context_window.
CONTEXT_WINDOWS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
}

_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}
_PRICE_KEYS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "cache_creation_input_token_cost_above_1hr",
    "max_input_tokens",
)


def usage_for_events(events_path):
    """Token totals, context, estimated cost and burn rate for one crew's native session.

    Cumulative over every transcript the events file has ever named, so a crew that runs
    /clear (new session id, new transcript) keeps the spend from the earlier one. Claude
    hooks name a transcript outright; Codex notifications name a thread whose rollout log
    we resolve. Returns None when neither yields a usable turn. Never raises, and never
    blocks on the network.
    """
    # ponytail: re-reads every transcript whole on each call, so a long session re-parses
    # a large file each refresh tick; carry byte offsets like read_cursor if it drags.
    transcripts = _transcript_paths(events_path)
    if not transcripts:
        return _codex_usage(events_path)

    prices = _prices()
    totals = dict.fromkeys(_FIELDS, 0)
    cost, spend, last = 0.0, [], None
    seen = set()
    for transcript in transcripts:
        for record in _records(transcript):
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            # One API response is written once per content block (thinking, then each
            # tool_use) and every copy repeats the whole response's usage.
            call = message.get("id")
            if isinstance(call, str):
                if call in seen:
                    continue
                seen.add(call)
            counts = {name: _count(usage.get(field)) for name, field in _FIELDS.items()}
            for name, value in counts.items():
                totals[name] += value
            # Synthetic turns report all-zero usage; they would blank the context.
            if not any(counts.values()):
                continue
            model = message.get("model")
            last = (counts, model)
            price = _for_model(prices, model)
            if price is None:
                cost = None
            elif cost is not None:
                turn = _turn_cost(price, counts, _write_1h(usage))
                cost += turn
                stamp = _epoch(record.get("timestamp"))
                if stamp is not None:
                    spend.append((stamp, turn))

    if last is None:
        return None
    counts, model = last
    return _frame(totals, _context(counts), context_limit(model), model, cost, spend)


def _codex_usage(events_path):
    """Token totals for the Codex rollout log the newest notification points at."""
    rollout = _rollout_path(events_path)
    if rollout is None:
        return None

    prices = _prices()
    totals = last = model = limit = None
    cost, spend = 0.0, []
    for record in _records(rollout):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        # A model switched inside the TUI opens the next turn with the new id.
        if record.get("type") == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        if isinstance(info.get("total_token_usage"), dict):
            totals = info["total_token_usage"]
            last = info.get("last_token_usage")
            # Per-turn usage sums exactly to the totals, so pricing turn by turn keeps a
            # mid-session model switch honest and feeds $/h from the same numbers.
            price = _for_model(prices, model)
            if price is None:
                cost = None
            elif cost is not None:
                turn = _turn_cost(price, _codex_counts(last), 0)
                cost += turn
                stamp = _epoch(record.get("timestamp"))
                if stamp is not None:
                    spend.append((stamp, turn))
        limit = _count(info.get("model_context_window")) or limit

    if totals is None:
        return None
    # Codex undercounts turns over 272k tokens, which bill at double rate; rare, skipped.
    return _frame(
        _codex_counts(totals),
        _context(_codex_counts(last)),
        _configured_limit() or limit,
        model,
        cost,
        spend,
    )


def _frame(totals, context, limit, model, cost, spend):
    return {
        **totals,
        "tokens": sum(totals.values()),
        "context": context,
        "limit": limit,
        "model": model,
        "cost": cost,
        "rate": _rate(spend) if cost is not None else None,
    }


def _context(counts):
    """Context used by the last turn. Input-only, matching Claude Code's used_percentage."""
    return counts["input"] + counts["cache_read"] + counts["cache_write"]


def _codex_counts(usage):
    """Codex counts cached prompt tokens inside input_tokens; the columns must not double up."""
    usage = usage if isinstance(usage, dict) else {}
    cache_read = _count(usage.get("cached_input_tokens"))
    return {
        "input": max(_count(usage.get("input_tokens")) - cache_read, 0),
        "output": _count(usage.get("output_tokens")),
        "cache_read": cache_read,
        "cache_write": _count(usage.get("cache_write_input_tokens")),
    }


def _write_1h(usage):
    """The 1-hour slice of cache_creation_input_tokens.

    A 1-hour cache write bills at 2x the base input rate against a 5-minute write's 1.25x,
    so pricing the two as one number undercounts.
    """
    detail = usage.get("cache_creation")
    return _count(detail.get("ephemeral_1h_input_tokens")) if isinstance(detail, dict) else 0


def _turn_cost(price, counts, write_1h):
    """List-price USD for one API call, with write_1h of its cache writes at the 1-hour rate."""
    five_minute = price.get("cache_creation_input_token_cost", 0)
    return (
        counts["input"] * price.get("input_cost_per_token", 0)
        + counts["output"] * price.get("output_cost_per_token", 0)
        + counts["cache_read"] * price.get("cache_read_input_token_cost", 0)
        + max(counts["cache_write"] - write_1h, 0) * five_minute
        + write_1h * price.get("cache_creation_input_token_cost_above_1hr", five_minute)
    )


def _rate(spend):
    """USD/hour over the trailing RATE_WINDOW, or None when no turn carried a timestamp."""
    if not spend:
        return None
    cutoff = time.time() - RATE_WINDOW
    return sum(cost for stamp, cost in spend if stamp >= cutoff) * 3600 / RATE_WINDOW


def _epoch(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _rollout_path(events_path):
    """The rollout log for the newest notification naming a thread that has one.

    Codex notifies for its own title-generation turns too, but those run on a throwaway
    thread that is never written to disk, so resolving the id filters them out. An id
    matching more than one log is ambiguous and is skipped rather than guessed at.
    """
    for record in reversed(list(_records(events_path))):
        thread = record.get("thread-id")
        if not isinstance(thread, str) or not _THREAD_ID.fullmatch(thread):
            continue
        try:
            matches = list(CODEX_SESSIONS_ROOT.glob(f"*/*/*/rollout-*-{thread}.jsonl"))
        except OSError:
            return None
        if len(matches) == 1:
            return matches[0]
    return None


def model_for_events(events_path):
    """The newest "model" value recorded directly on a hook event. Never raises.

    Claude Code names transcript_path in SessionStart before the transcript file
    exists, so usage_for_events has nothing to read yet; the event itself already
    carries the model.
    """
    model = None
    for record in _records(events_path):
        value = record.get("model")
        if isinstance(value, str) and value:
            model = value
    return model


def context_limit(model):
    """The context window for a Claude `model`, or None when nothing covers it.

    An explicit [dashboard] context_limit wins, so an unlisted model is never stuck. Nothing
    Claude Code writes to disk states its window, so it comes from LiteLLM's
    max_input_tokens, falling back to CONTEXT_WINDOWS when the price cache is cold.
    Codex is excluded on purpose: LiteLLM reports the API window (922000 for
    gpt-5.6-terra) while the rollout log states the smaller window the CLI actually
    gives the crew (258400), and the native log is the authoritative one.
    """
    override = _configured_limit()
    if override is not None:
        return override
    if not model or not any(model.startswith(name) for name, _ in models.MODELS["claude"]):
        return None
    price = _for_model(_prices(), model) or {}
    return _count(price.get("max_input_tokens")) or _for_model(CONTEXT_WINDOWS, model)


def _for_model(table, model):
    """The entry of `table` owning `model`.

    Dated ids (claude-haiku-4-5-20251001) extend their base id, so the longest matching
    prefix is the entry that owns them.
    """
    matches = [name for name in table if model and model.startswith(name)]
    return table[max(matches, key=len)] if matches else None


def _prices():
    """Per-token prices for the models we launch, keyed by model id.

    Empty until a cached extract or a [dashboard] prices_file exists, which is how an
    unknown cost stays unknown: the frame renders `$?` rather than a confident $0.
    """
    override = config.text("dashboard", "prices_file")
    if override:
        # Set in a file, not a shell, so nothing has expanded ~ on the way in.
        return _read_json(Path(override).expanduser())
    cache = _prices_cache()
    if cache is None:
        return {}
    extract = _read_json(cache)
    if not extract or _age(cache) > PRICES_TTL:
        _start_refresh(cache)
    return extract


def _prices_cache():
    try:
        return memory.private_dir(memory.state_root()) / "prices.json"
    except (memory.CaptainError, OSError):
        return None


def _age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return PRICES_TTL + 1


_refresh_lock = threading.Lock()
_refreshed = False


def _start_refresh(cache):
    """Refresh off the refresh loop, so no frame ever waits on the network."""
    global _refreshed
    with _refresh_lock:
        if _refreshed:
            return
        # ponytail: one attempt per process, so an offline dashboard does not retry every
        # frame; restart it to try again once the network is back.
        _refreshed = True
    threading.Thread(target=_refresh_prices, args=(cache,), daemon=True).start()


def _refresh_prices(cache):
    """Keep only the models models.MODELS launches; the published file is 2.8MB of 4300."""
    launchable = {model for entries in models.MODELS.values() for model, _ in entries}
    try:
        with urllib.request.urlopen(PRICES_URL, timeout=30) as response:  # noqa: S310
            table = json.loads(response.read().decode("utf-8"))
        extract = {
            name: {key: entry[key] for key in _PRICE_KEYS if key in entry}
            for name, entry in table.items()
            if name in launchable and isinstance(entry, dict)
        }
        if extract:
            memory.write_json(cache, extract)
    except Exception:
        # A price refresh must never surface a traceback into the dashboard pane.
        return


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured_limit():
    """[dashboard] context_limit, or None when it is left at 0 to be derived."""
    return config.lookup("dashboard", "context_limit", kind=int) or None


def _transcript_paths(events_path):
    """Every transcript the events file has named, oldest first, deduplicated.

    A crew that runs /clear gets a new session id and a new transcript file; the spend
    before it only exists in the earlier one, and the total must never shrink.
    """
    paths = {}
    for record in _records(events_path):
        path = record.get("transcript_path")
        if isinstance(path, str) and path:
            paths[path] = None
    return list(paths)


def _records(path):
    """Yield the JSON objects of a JSONL file, skipping anything unreadable."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
