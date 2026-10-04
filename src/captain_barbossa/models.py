"""Per-agent model tables and text matching for crew launches."""

import json
from collections import Counter
from difflib import get_close_matches
from functools import lru_cache

from . import config, runtime
from .runtime import CaptainError

# Cheapest to strongest per agent CLI; aliases are the short names people say.
MODELS = {
    "claude": (
        ("claude-haiku-4-5", ("haiku",)),
        ("claude-sonnet-5", ("sonnet",)),
        ("claude-opus-5", ("opus",)),
        ("claude-fable-5-1", ("fable",)),
    ),
    "codex": (
        ("gpt-5.6-luna", ("luna",)),
        ("gpt-5.6-terra", ("terra",)),
        ("gpt-5.6-sol", ("sol",)),
        ("gpt-5.5", ()),
        ("gpt-6-astra", ("astra",)),
    ),
    # Aliases are the labels Grok shows and echoes when it confirms a switch ("Grok 4.6").
    "grok": (
        ("grok-4.5", ("grok 4.5",)),
        ("grok-4.7-build-fast", ("grok 4.7 build fast",)),
        ("grok-4.6", ("grok 4.6",)),
        ("grok-4.7", ("grok 4.7",)),
    ),
}

# pi is absent above on purpose: it is provider-agnostic, so its catalog is whatever
# the user has authenticated locally and is discovered by pi_models() instead.
PROVIDERS = ("claude", "codex", "pi", "grok")
# The stop reasons pi reports for a turn the model actually finished.
PI_SETTLED = ("stop", "end_turn")
# No hook or notify mechanism, so crew state comes from Herdr's own agent status and the
# pane, and a switch or a mail doorbell has to read the composer to know it is safe.
HOOKLESS = ("pi", "grok")
# Providers whose own hook can hand mail to their own model, so the body never crosses a
# terminal. Codex's notify marks a turn boundary but cannot return context, so it stays out.
HOOK_DELIVERED = ("claude",)
# Provider-neutral tiers: the captain picks one from the task, each CLI resolves its own.
# defaults.toml is the one source for these, so settings can layer over the same values.
TIERS = {provider: dict(tiers) for provider, tiers in config.defaults()["models"].items()}
TIER_NAMES = ("cheap", "mid", "strong")


def _size(value):
    """A pi table size cell ("272K", "16.4K") as a number; 0 when unparseable."""
    scale = {"K": 1e3, "M": 1e6}.get(value[-1:], 1)
    try:
        return float(value.rstrip("KM")) * scale
    except ValueError:
        return 0.0


@lru_cache(maxsize=1)
def pi_models():
    """This pi install's authenticated models, weakest to strongest.

    `pi --list-models` prints a fixed-width table (provider, model, context, max-out,
    thinking, images) and exposes no pricing, so the ranking uses the capability
    columns it does give: thinking support, then context, then max output, with pi's
    own ordering breaking ties. IDs are `provider/model`, the exact form pi's --model
    and /model accept without opening their picker.
    """
    try:
        result = runtime.subprocess.run(
            [runtime.executable("pi"), "--list-models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        failure = result.stderr.strip() if result.returncode else ""
    except runtime.HERDR_ERRORS as exc:
        failure = str(exc) or exc.__class__.__name__
        result = None
    rows = []
    for line in result.stdout.splitlines() if result else ():
        fields = line.split()
        if len(fields) < 5 or fields[0] == "provider":
            continue
        provider, model, context, max_out, thinking = fields[:5]
        rows.append(((thinking == "yes", _size(context), _size(max_out)), provider, model))
    if failure or not rows:
        detail = f": {failure.rstrip('.')}" if failure else ""
        raise CaptainError(
            f"Could not read pi's model list{detail}. "
            "Run `pi --list-models` yourself to check pi is installed and a provider "
            "is authenticated."
        )
    rows.sort(key=lambda row: row[0])
    bare = Counter(model for _, _, model in rows)
    # The short name is an alias only when one provider offers it; otherwise it stays
    # ambiguous so resolve_model asks rather than guessing a provider.
    return tuple(
        (f"{provider}/{model}", (model,) if bare[model] == 1 else ()) for _, provider, model in rows
    )


def models_for(provider):
    return pi_models() if provider == "pi" else MODELS[provider]


def tiers_for(provider):
    """The built-in tiers, with any tier named in [models.<provider>] settings replacing one.

    Settings may name a model however the user says it, so aliases are mapped here rather
    than through resolve_model, which calls this and would recurse.
    """
    if provider == "pi":
        ids = model_ids(provider)
        tiers = dict(zip(TIER_NAMES, (ids[0], ids[len(ids) // 2], ids[-1])))
    else:
        tiers = dict(TIERS[provider])
    names = {name: model for model, aliases in models_for(provider) for name in (model, *aliases)}
    for tier in TIER_NAMES:
        choice = config.text("models", provider, tier)
        if choice:
            tiers[tier] = names.get(normalized(choice), choice)
    return tiers


def model_ids(provider):
    return [model for model, _ in models_for(provider)]


def model_names(provider, model):
    """A model's ID and aliases, for matching a native CLI's own confirmation text."""
    for name, aliases in models_for(provider):
        if name == model:
            return (name, *aliases)
    return (model,)


def claude_label(model):
    """How Claude Code's own picker lists a model ID: "claude-haiku-4-5" is "Haiku 4.5".

    Its rows carry display names, and several versions of one family at once, so picking
    a row needs the version too: an alias alone also matches "Opus 4.7" for opus.
    """
    family, _, version = model.removeprefix("claude-").partition("-")
    return f"{family} {version.replace('-', '.')}".strip()


def normalized(text):
    """Free text as a lookup key: casefolded, with spaces and underscores as dashes."""
    return "-".join(text.casefold().split()).replace("_", "-").strip("-")


def resolve_model(provider, text):
    """Map a tier or free text to a model ID for provider, or raise listing the options."""
    # A blank or unset setting is a user mistake to report, not a crash: None reaches
    # here whenever a settings key is left empty.
    wanted = normalized(text or "")
    names = {}
    tiers = tiers_for(provider)
    for model, aliases in models_for(provider):
        for name in (model, *aliases):
            names[name] = model
    if not wanted:
        raise CaptainError(
            f"Provide a tier ({'|'.join(TIER_NAMES)}) or model name. "
            f"{provider} models: {', '.join(model_ids(provider))}."
        )
    if wanted in tiers:
        return tiers[wanted]
    if wanted in names:
        return names[wanted]
    for match in (
        [name for name in names if name.startswith(wanted)],
        [name for name in names if wanted in name],
        get_close_matches(wanted, list(names), n=3, cutoff=0.6),
    ):
        found = list(dict.fromkeys(names[name] for name in match))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise CaptainError(
                f"Model '{text}' is ambiguous for {provider}: {', '.join(found)}. Ask the user which."
            )
    raise CaptainError(
        f"No {provider} model matches '{text}'. Tiers: {', '.join(TIER_NAMES)}. "
        f"Options: {', '.join(model_ids(provider))}."
    )


def native_model_args(provider, model):
    if not model:
        return []
    return ["-m", model] if provider == "codex" else ["--model", model]


def headless_argv(provider, binary, task, *, writable=False, add_dir=None, model=None):
    """The whole literal argv for one paneless turn, built per provider, never composed.

    Each CLI gets the strongest gate it actually has rather than one forced shape, and
    every element here is written by this function: nothing a caller passes becomes a flag.
    """
    if provider == "claude":
        argv = [binary, "-p", task, "--output-format", "json", "--permission-mode", "dontAsk"]
        argv += ["--allowedTools", "Read", "Grep", "Glob"]
        if writable:
            argv += ["Edit", "Write"]
        else:
            argv += ["--disallowedTools", "Bash", "Write", "Edit"]
            # Claude path-checks reads against its working directories; the others read the
            # filesystem already, so this is the one provider that has to be told.
            argv += ["--add-dir", add_dir] if add_dir else []
        return argv + native_model_args(provider, model)
    if provider == "codex":
        # --ignore-user-config because a trusted project or approvals_reviewer in the user's
        # own config.toml silently defeats --sandbox; auth still comes from CODEX_HOME. No
        # --add-dir: for Codex that flag grants writes, and its sandbox already reads freely.
        return [
            binary,
            "exec",
            "--json",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write" if writable else "read-only",
            *native_model_args(provider, model),
            task,
        ]
    if provider == "grok":
        argv = [binary, "--trust", "-p", task, "--output-format", "json"]
        argv += ["--permission-mode", "dontAsk", "--deny", "Bash"]
        if not writable:
            argv += ["--deny", "Write", "--deny", "Edit"]
        return argv + native_model_args(provider, model)
    # pi silently drops a tool name it does not know, so only names pi documents are used.
    tools = "read,write,edit" if writable else "read"
    return [
        binary,
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--no-context-files",
        "--tools",
        tools,
        *native_model_args(provider, model),
        task,
    ]


def _lines(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _incomplete(provider, model, detail):
    turn = f"The {provider} turn on {model or 'its default model'}"
    raise CaptainError(f"{turn} did not complete: {detail or 'no result document'}")


def headless_report(provider, stdout, model=None):
    """One turn's own text and a cost note, accepted only from a complete success.

    Every provider exits 0 on some failed turn - pi does it for every provider error - so
    the result document decides and the exit status never does. A shape this does not
    recognise is a failure, so a half-finished turn can never read as an answer.
    """
    if provider in ("claude", "grok"):
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            _incomplete(provider, model, stdout[-400:])
        if provider == "claude":
            done = (
                payload.get("type") == "result"
                and payload.get("subtype") == "success"
                and payload.get("is_error") is False
                and isinstance(payload.get("result"), str)
            )
            text = payload.get("result")
        else:
            done = payload.get("stopReason") == "end_turn" and isinstance(payload.get("text"), str)
            text = payload.get("text")
        if not done or not str(text).strip():
            _incomplete(
                provider, model, str(payload.get("message") or payload.get("subtype") or text)
            )
        return text, _note(payload.get("num_turns"), payload.get("total_cost_usd"))
    if provider == "codex":
        messages, settled, failure = [], None, None
        for event in _lines(stdout):
            kind, item = event.get("type"), event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text") or "")
            elif kind == "turn.completed":
                settled = event.get("usage") or {}
            elif kind in ("turn.failed", "error"):
                failure = (event.get("error") or {}).get("message") or event.get("message")
        if settled is None or not messages or not messages[-1].strip():
            _incomplete(provider, model, failure)
        # Codex narrates before it answers, so the last message is the answer.
        return messages[-1], _note(None, None, settled.get("output_tokens"))
    final = None
    for event in _lines(stdout):
        if event.get("type") == "turn_end":
            final = event.get("message") or {}
    if final is None:
        _incomplete(provider, model, None)
    # pi relays its provider's own word: a live xai turn finished with "stop", and the
    # values that must never read as an answer are "error", "toolUse" and a truncation.
    if final.get("stopReason") not in PI_SETTLED:
        diagnostics = final.get("diagnostics") or [{}]
        _incomplete(
            provider,
            model,
            final.get("errorMessage")
            or (diagnostics[-1].get("error") or {}).get("message")
            or final.get("stopReason"),
        )
    text = "".join(
        block.get("text") or "" for block in final.get("content") or [] if isinstance(block, dict)
    )
    if not text.strip():
        _incomplete(provider, model, "the turn ended with no text")
    usage = final.get("usage") or {}
    return text, _note(None, (usage.get("cost") or {}).get("total"), usage.get("totalTokens"))


def _note(turns, cost, tokens=None):
    """The one line of spend a paneless turn owes the captain, from whatever it reported."""
    parts = [f"{turns} turns" if turns else None]
    parts += [f"${cost:.4f}" if isinstance(cost, (int, float)) and cost else None]
    parts += [f"{tokens} output tokens" if tokens else None]
    return "(" + ", ".join(part for part in parts if part) + ")" if any(parts) else ""
