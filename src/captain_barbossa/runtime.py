"""External CLI execution and Herdr workspace discovery."""

import json
import os
import shutil
import subprocess


class CaptainError(Exception):
    pass


def executable(name):
    path = shutil.which(name)
    if not path:
        raise CaptainError(f"{name} is not installed or is missing from PATH.")
    return path


def herdr(*args, timeout=15, expect_output=True, raw=False):
    result = subprocess.run(
        [executable("herdr"), *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise CaptainError(result.stderr.strip() or result.stdout.strip() or "Herdr failed.")
    if raw:
        # Terminal reads are plain text, not the JSON envelope every other command returns.
        return result.stdout
    if not expect_output and not result.stdout:
        return {}
    try:
        response = json.loads(result.stdout)
        if "error" in response:
            raise CaptainError(f"Herdr: {response['error']}")
        payload = response["result"]
        if not isinstance(payload, dict):
            raise ValueError("result must be an object")
        for key in ("pane", "root_pane", "agent", "layout"):
            if key in payload and not isinstance(payload[key], dict):
                raise ValueError(f"{key} must be an object")
        return payload
    except (ValueError, KeyError, TypeError) as exc:
        raise CaptainError(
            f"Herdr returned an unexpected response for herdr {' '.join(args[:2])}; "
            f"check its version. Raw stdout:\n{result.stdout}\nStderr:\n{result.stderr}"
        ) from exc


def current_pane():
    if not all(
        os.environ.get(key) for key in ("HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID")
    ):
        raise CaptainError("Run captain inside a Herdr workspace.")
    # Resolve the live pane: Herdr's launch-time IDs can be stale after a move.
    result = herdr("pane", "current", "--current")
    pane = result.get("pane", {})
    if not all(pane.get(key) for key in ("workspace_id", "tab_id", "pane_id")):
        raise CaptainError("Could not identify the current Herdr workspace and pane.")
    return pane
