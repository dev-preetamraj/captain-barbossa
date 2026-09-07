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
# Smart select tiers: cheapest, mid, strongest.
SMART = {"claude": ("haiku", "sonnet", "opus"), "codex": ("spark", "terra", "astra")}


def model_ids(provider):
    return [model for model, _ in MODELS[provider]]


def resolve_model(provider, text):
    """Map free text to the closest model ID for provider, or raise listing the options."""
    wanted = "-".join(text.casefold().split()).replace("_", "-").strip("-")
    names = {}
    for model, aliases in MODELS[provider]:
        for name in (model, *aliases):
            names[name] = model
    if not wanted:
        raise CaptainError(
            f"Provide a model name. {provider} models: {', '.join(model_ids(provider))}."
        )
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
        f"No {provider} model matches '{text}'. Options: {', '.join(model_ids(provider))}."
    )


def native_model_args(provider, model):
    if not model:
        return []
    return ["--model", model] if provider == "claude" else ["-m", model]
