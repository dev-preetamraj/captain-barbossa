"""Explicit recruitment presets; assignment permissions never come from settings."""

from copy import copy

from . import config
from .models import PROVIDERS
from .runtime import CaptainError


def apply(args):
    """Return recruitment arguments with the selected profile below explicit flags."""
    name = getattr(args, "profile", None)
    if name is None:
        return args, {}
    table = config.lookup("profiles")
    if not isinstance(table, dict) or name not in table:
        raise CaptainError(f"Unknown recruitment profile {name!r}; define [profiles.{name}].")
    profile = table[name]
    choices = {
        "agent": PROVIDERS,
        "model": None,
        "execution": ("foreground", "background"),
        "checkout": ("auto", "shared", "worktree"),
        "placement": ("auto", "pane", "tab"),
    }
    if not isinstance(profile, dict):
        raise CaptainError(f"[profiles.{name}] must be a table.")
    for key, value in profile.items():
        where = config.source("profiles", name, key) or "settings"
        if key not in choices:
            raise CaptainError(f"[profiles.{name}] unknown key {key!r} from {where}.")
        if not isinstance(value, str) or not value.strip():
            raise CaptainError(f"[profiles.{name}] {key} must be a nonempty string from {where}.")
        if choices[key] is not None and value not in choices[key]:
            raise CaptainError(f"[profiles.{name}] invalid {key} {value!r} from {where}.")
    effective, sources = copy(args), {}
    for key, value in profile.items():
        attribute = {"agent": "crew_agent", "execution": "background"}.get(key, key)
        if key == "execution":
            explicit = getattr(args, "background", False) or getattr(args, "foreground", False)
        elif key == "placement":
            explicit = args.placement or args.direction or args.split_pane
        else:
            explicit = getattr(args, attribute, None)
        if not explicit:
            setattr(effective, attribute, value == "background" if key == "execution" else value)
            where = config.source("profiles", name, key) or "settings"
            sources[key] = f"[profiles.{name}] {key} from {where}"
    return effective, sources
