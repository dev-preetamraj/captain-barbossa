"""Bounded subprocess runs that reap their whole process group, and the Makefile targets a project declares."""

import os
import re
import signal
import subprocess
import time

from .runtime import CaptainError

# A declared target may run a whole test suite.
TIMEOUT = 900


# Let commands flush or clean up before a stuck process group is killed.
GROUP_TERMINATE_TIMEOUT = 1.0


TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def terminate_process_group(process):
    """Stop and reap the command and every descendant still in its process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + GROUP_TERMINATE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.communicate(timeout=GROUP_TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # A descendant may have escaped with setsid while retaining these pipe descriptors.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        try:
            process.wait(timeout=GROUP_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


def capture(root, command, timeout=TIMEOUT, *, text=True):
    """Run a bounded command, raising only when it could not run at all.

    Kept apart from `_spawn` so a headless turn can read stdout on its own: its result is
    JSON, which anything the tool writes to stderr would make unparseable.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CaptainError(f"{command[0]} is not installed.") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        raise CaptainError(f"{command[0]} exceeded {timeout}s.") from exc
    except KeyboardInterrupt:
        terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def makefile_targets(root):
    """Target names the project declares in its own Makefile."""
    makefile = root / "Makefile"
    if not makefile.is_file() or makefile.is_symlink():
        raise CaptainError("No Makefile at the project root; `do run` has nothing to call.")
    found = set()
    for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:(?!=)", line)
        if match:
            found.add(match.group(1))
    return found
