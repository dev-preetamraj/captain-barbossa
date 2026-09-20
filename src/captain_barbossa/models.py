"""Per-agent model tables and text matching for crew launches."""

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
}
# pi is absent above on purpose: it is provider-agnostic, so its catalog is whatever
# the user has authenticated locally and is discovered by pi_models() instead.
PROVIDERS = ("claude", "codex", "pi")
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


def normalized(text):
    """Free text as a lookup key: casefolded, with spaces and underscores as dashes."""
    return "-".join(text.casefold().split()).replace("_", "-").strip("-")


def resolve_model(provider, text):
    """Map a tier or free text to a model ID for provider, or raise listing the options."""
    wanted = normalized(text)
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
