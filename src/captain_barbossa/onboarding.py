"""Bootstrap Herdr when captain is launched outside a workspace."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import questionary

from . import runtime
from .memory import project_root, storage
from .prompts import STYLE
from .runtime import HERDR_ERRORS, CaptainError

INSTALL_URL = "https://herdr.dev/install.sh"
INSTALL_BIN = os.path.expanduser("~/.local/bin")
SERVER_TIMEOUT = 20
POLL_SECONDS = 0.3


def install():
    print(f"Installing Herdr from {INSTALL_URL}")
    fd, script = tempfile.mkstemp(prefix="herdr-install-", suffix=".sh")
    os.close(fd)
    try:
        # A piped `curl | sh` reports sh's exit code, not curl's, so a truncated
        # download can look like success; download first and check that separately.
        if subprocess.run(["curl", "-fsSL", "-o", script, INSTALL_URL], timeout=600).returncode:
            raise CaptainError("The Herdr installer failed. Install Herdr from https://herdr.dev.")
        if subprocess.run(["sh", script], timeout=600).returncode:
            raise CaptainError("The Herdr installer failed. Install Herdr from https://herdr.dev.")
    finally:
        os.unlink(script)
    # The installer drops herdr in ~/.local/bin; append so it never shadows system dirs.
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(filter(None, [path, INSTALL_BIN]))
    if not shutil.which("herdr"):
        raise CaptainError(f"Herdr installed, but no herdr binary is in PATH or {INSTALL_BIN}.")


def start_server():
    """Herdr's socket API only answers once a server runs; start a headless one if needed."""
    deadline = time.monotonic() + SERVER_TIMEOUT
    started = False
    while True:
        try:
            runtime.herdr("workspace", "list")
            return
        except HERDR_ERRORS:
            if not started:
                subprocess.Popen(
                    [runtime.executable("herdr"), "server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                started = True
            if time.monotonic() >= deadline:
                raise CaptainError("Herdr's server did not start. Run `herdr`, then `captain`.")
            time.sleep(POLL_SECONDS)


def open_workspace(args, project):
    """Create a workspace at the project, start captain in its root pane, attach this terminal."""
    start_server()
    created = runtime.herdr(
        "workspace",
        "create",
        "--cwd",
        str(project),
        "--label",
        project.name,
        "--focus",
        "--env",
        "CAPTAIN_BOOTSTRAPPED=1",
    )
    pane = created.get("root_pane", {}).get("pane_id")
    if not pane:
        raise CaptainError("Herdr created a workspace but returned no pane ID.")
    # argv[0] can be a relative path or a non-executable __main__.py under `python -m`,
    # neither of which the new pane can exec; re-enter through this interpreter.
    command = [sys.executable, "-m", "captain_barbossa"]
    for flag, value in (
        ("--agent", args.agent),
        ("--session", args.session),
        ("--prompt", args.prompt),
    ):
        if value:
            command += [flag, value]
    launcher = storage(project) / "onboard.sh"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.join(command)}\n", encoding="utf-8")
    launcher.chmod(0o600)
    # A new pane may still be in canonical mode: keep terminal input short.
    runtime.herdr("pane", "run", pane, f'/bin/sh "{launcher}"', expect_output=False)
    binary = runtime.executable("herdr")
    os.execv(binary, [binary])


def bootstrap(args):
    """Offer to install Herdr or open a workspace. Returns when declined, so the caller
    still hits the same errors captain raised before onboarding existed."""
    if os.environ.get("CAPTAIN_BOOTSTRAPPED"):
        # The captain launched into the new pane is the same command re-run from scratch,
        # so it hits this same function; the env var stops it from bootstrapping again.
        return
    if shutil.which("herdr"):
        if runtime.in_workspace():
            return
        question = "Herdr is not running here. Open a Herdr workspace for this project?"
    else:
        question = f"Herdr is not installed. Install it with `curl -fsSL {INSTALL_URL} | sh`?"
    if not sys.stdin.isatty():
        return
    if not questionary.confirm(question, default=False, style=STYLE).unsafe_ask():
        return
    if not shutil.which("herdr"):
        install()
    open_workspace(args, project_root())
