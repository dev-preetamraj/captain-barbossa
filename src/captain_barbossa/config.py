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


def defaults_text():
    """The shipped defaults file, read as package data so it works from a wheel."""
    return files(__package__).joinpath("defaults.toml").read_text(encoding="utf-8")


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


def project_settings():
    """The project's settings path, or None when no project can be resolved."""
    try:
        return project_root() / SETTINGS_PATH
    except HERDR_ERRORS:
        # Reading settings must not be what reports an undiscoverable project: the
        # command itself does that, and model lookups happen well before it.
        return None


@lru_cache(maxsize=1)
def settings():
    """defaults.toml under the home file under the project's; both user files are optional."""
    merged = merge(defaults(), read(Path.home() / SETTINGS_PATH))
    project = project_settings()
    return merge(merged, read(project)) if project else merged


def source(*names):
    """The settings file that set names, or None when it is the shipped default.

    Only for error messages, so it re-reads rather than tracking provenance through the
    merge: the cost lands on the path that is about to fail anyway.
    """
    for path in (project_settings(), Path.home() / SETTINGS_PATH):
        found = read(path) if path else {}
        for name in names:
            found = found.get(name) if isinstance(found, dict) else None
        if found is not None:
            return path
    return None


def _typed(table, names, kind):
    """Walk names into table and return the value only if it is already a kind."""
    found = table
    for name in names:
        if not isinstance(found, dict):
            return None
        found = found.get(name)
    if isinstance(found, bool) and kind not in (bool, object):
        return None
    if kind is float and isinstance(found, int):
        return float(found)
    return found if isinstance(found, kind) else None


def lookup(*names, kind=object):
    """The value at names (e.g. "dashboard", "interval") as a kind, else the shipped default.

    Nothing is ever coerced across types: TOML has real booleans, so `true` is a switch
    and `"true"` stays a string, and a number is never read out of one either. The one
    latitude is an int where a float is wanted, since 2 and 2.0 are the same interval to
    everyone but the parser. A value of the wrong type is ignored, which leaves the
    default from defaults.toml standing rather than a None the caller has to handle.
    """
    found = _typed(settings(), names, kind)
    return _typed(defaults(), names, kind) if found is None else found


def text(*names):
    """The string at names, or None when it is unset, blank, or another type."""
    found = lookup(*names, kind=str)
    return found.strip() or None if isinstance(found, str) else None


def flag(*names):
    """The boolean at names, or None when nothing of that name is a boolean."""
    return lookup(*names, kind=bool)


def in_range(value, what, least=None, below=None, where=""):
    """value, refused unless it clears the floor (above 0, or `least`) and stays under `below`.

    An interval, a timeout and a split ratio all reach a sleep or Herdr, where a
    negative is not a smaller setting but a crash or a dead pane. A flag is checked the
    same way a file is, so neither is the unchecked way in.
    """
    if not (value >= least if least is not None else value > 0) or (
        below is not None and value >= below
    ):
        floor = f"at least {least}" if least is not None else "above 0"
        limit = f"{floor} and below {below}" if below is not None else floor
        raise CaptainError(f"{what} must be a number {limit}. Got {value!r}{where}.")
    return value


def number(*names, kind=float, below=None):
    """The number at names, refused when it is out of range, naming the file that set it."""
    path = source(*names)
    return in_range(
        lookup(*names, kind=kind),
        f"[{names[0]}] {'.'.join(names[1:])}",
        below=below,
        where=f" from {path}" if path else "",
    )


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
