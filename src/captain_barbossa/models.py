"""Per-agent model tables and text matching for crew launches."""

from difflib import get_close_matches

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
        ("gpt-5.3-codex-spark", ("spark",)),
        ("gpt-5.4-mini", ("mini",)),
        ("gpt-5.6-luna", ("luna",)),
        ("gpt-5.6-terra", ("terra",)),
        ("gpt-5.6-sol", ("sol",)),
        ("gpt-5.5", ()),
        ("gpt-6-astra", ("astra",)),
    ),
}
# Provider-neutral tiers: the captain picks one from the task, each CLI resolves its own.
TIERS = {
    "claude": {"cheap": "claude-haiku-4-5", "mid": "claude-sonnet-5", "strong": "claude-opus-5"},
    "codex": {"cheap": "gpt-5.3-codex-spark", "mid": "gpt-5.6-terra", "strong": "gpt-6-astra"},
}
TIER_NAMES = ("cheap", "mid", "strong")


def model_ids(provider):
    return [model for model, _ in MODELS[provider]]


def model_names(provider, model):
    """A model's ID and aliases, for matching a native CLI's own confirmation text."""
    for name, aliases in MODELS[provider]:
        if name == model:
            return (name, *aliases)
    return (model,)


def resolve_model(provider, text):
    """Map a tier or free text to a model ID for provider, or raise listing the options."""
    wanted = "-".join(text.casefold().split()).replace("_", "-").strip("-")
    names = {}
    for model, aliases in MODELS[provider]:
        for name in (model, *aliases):
            names[name] = model
    if not wanted:
        raise CaptainError(
            f"Provide a tier ({'|'.join(TIER_NAMES)}) or model name. "
            f"{provider} models: {', '.join(model_ids(provider))}."
        )
    if wanted in TIERS[provider]:
        return TIERS[provider][wanted]
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
    return ["--model", model] if provider == "claude" else ["-m", model]
