"""Startup PyPI update check for captain-barbossa."""

import json
import subprocess
import urllib.request

import questionary

from . import __version__

PYPI_URL = "https://pypi.org/pypi/captain-barbossa/json"
TIMEOUT = 2


def _latest_version():
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=TIMEOUT) as response:
            return json.load(response)["info"]["version"]
    except Exception:
        # A flaky network or a PyPI response shape change must never block launch.
        return None


def _is_newer(latest, installed):
    try:
        return tuple(map(int, latest.split("."))) > tuple(map(int, installed.split(".")))
    except ValueError:
        return False


def check_for_update():
    """Prompt to upgrade if a newer release exists on PyPI; never raises."""
    latest = _latest_version()
    if not latest or not _is_newer(latest, __version__):
        return
    try:
        upgrade = questionary.confirm(
            f"captain-barbossa {latest} is available (installed {__version__}). "
            "Run 'uv tool upgrade captain-barbossa' now?",
            default=False,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        return
    if upgrade:
        subprocess.run(["uv", "tool", "upgrade", "captain-barbossa"])
