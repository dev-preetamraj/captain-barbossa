"""User settings: the project's .captain/settings.toml over the home one over defaults.toml.

This module must not import models: models reads its tier table from the defaults here.
"""

import tomllib
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .memory import project_root
from .runtime import HERDR_ERRORS, CaptainError

SETTINGS_PATH = Path(".captain") / "settings.toml"
DEFAULTS_NAME = "defaults.toml"


@lru_cache(maxsize=1)
def defaults_text():
    """The shipped defaults file, read as package data so it works from a wheel."""
    return files(__package__).joinpath(DEFAULTS_NAME).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def defaults():
    """Every built-in default, and the bottom layer of settings()."""
    return tomllib.loads(defaults_text())


def read(path):
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CaptainError(f"Could not read settings from {path}: {exc}") from exc


def merge(base, overlay):
    """Overlay onto base a table at a time; any other value replaces what it lands on."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def settings():
    """defaults.toml under the home file under the project's; both user files are optional."""
    merged = merge(defaults(), read(Path.home() / SETTINGS_PATH))
    try:
        project = project_root()
    except HERDR_ERRORS:
        # Reading settings must not be what reports an undiscoverable project: the
        # command itself does that, and model lookups happen well before it.
        return merged
    return merge(merged, read(project / SETTINGS_PATH))


def lookup(*names):
    """The raw value at names, or None when a step is missing or is not a table."""
    found = settings()
    for name in names:
        if not isinstance(found, dict):
            return None
        found = found.get(name)
    return found


def text(*names):
    """The string at names (e.g. "captain", "model"), or None when unset or another type."""
    found = lookup(*names)
    return found.strip() or None if isinstance(found, str) else None


def flag(*names):
    """The boolean at names, or None when unset or another type.

    TOML already has real booleans, so "true" is a string and stays one: coercing it
    would quietly accept a typo that the user meant as a switch.
    """
    found = lookup(*names)
    return found if isinstance(found, bool) else None


def template():
    """defaults.toml with every value commented out, so writing it changes nothing.

    Generated rather than written out again so the template cannot drift from the
    defaults; it holds because every value in defaults.toml fits on one line.
    """
    lines = [
        "# Captain Barbossa settings. Every setting is commented out at the value in",
        "# force today, so this file changes nothing until you uncomment a line.",
        "# A project file overrides ~/.captain/settings.toml, which overrides the",
        "# shipped defaults, one key at a time: whatever stays commented falls back.",
        "",
    ]
    for line in defaults_text().splitlines():
        stripped = line.strip()
        lines.append(f"# {line}" if stripped and not stripped.startswith(("#", "[")) else line)
    return "\n".join(lines).rstrip("\n") + "\n"


def init_settings(args):
    """Create the project (or, with --global, the home) settings file if it is missing."""
    path = (Path.home() if args.home else project_root()) / SETTINGS_PATH
    if path.exists():
        print(f"Settings already exist, leaving them alone: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template(), encoding="utf-8")
    print(f"Wrote {path}")
