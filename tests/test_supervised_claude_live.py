"""Opt-in real Claude smoke test through the Agent SDK; it uses the user's own claude login."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("CAPTAIN_LIVE_CLAUDE") == "1", "real Claude is opt-in")
class LiveClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="captain-live-"))
        ).resolve()
        self.project = self.scratch / "project"
        self.project.mkdir()
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("CAPTAIN_")
        }
        self.env.update(
            CAPTAIN_MEMORY_ROOT=str(self.scratch / "state"),
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        )
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, env=self.env)
        self.command = [sys.executable, "-m", "captain_barbossa"]
        self.errors = self.enterContext(tempfile.TemporaryFile(mode="w+"))
        self.server = subprocess.Popen(
            [*self.command, "supervise"], cwd=self.project, env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.errors, text=True,
        )  # fmt: skip
        self.addCleanup(self.stop)
        self.assertTrue(select.select([self.server.stdout], [], [], 40)[0], "readiness timed out")
        line = self.server.stdout.readline()
        if not line:
            self.errors.seek(0)
            self.fail(self.errors.read())
        self.ready = json.loads(line)
        self.env["CAPTAIN_SUPERVISOR_TOKEN"] = self.ready["token"]

    def request(self, method: str, **params: object) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(40)
            connection.connect(self.ready["socket"])
            message = {"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": {"token": self.ready["token"], **params}}  # fmt: skip
            connection.sendall(json.dumps(message).encode() + b"\n")
            with connection.makefile("rb") as response:
                reply = json.loads(response.readline())
            self.assertNotIn("error", reply, reply)
            return reply["result"]

    def stop(self) -> None:
        try:
            if self.server.poll() is None:
                if hasattr(self, "ready"):
                    self.request("shutdown")
                else:
                    self.server.terminate()
                self.server.wait(timeout=60)
        finally:
            if self.server.poll() is None:
                self.server.kill()
                self.server.wait(timeout=10)
            self.server.stdout.close()

    def captain(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, "--session", self.ready["session_id"], *args],
            cwd=self.project, env=self.env, capture_output=True, text=True, timeout=90,
        )  # fmt: skip

    def test_real_claude_reports_done_through_its_own_hooks(self) -> None:
        started = self.captain(
            "crew", "Jack", "--agent", "claude", "--model", "claude-sonnet-5-5",
            "--task", "Run captain done with report 'live smoke complete'; do not edit files.",
            "--allow", "read",
        )  # fmt: skip
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(json.loads(started.stdout.splitlines()[-1])["status"], "started")
        statuses, ack, deadline = [], [], time.monotonic() + 240
        # Until done, then on with no wait, so every notification is acknowledged before dismiss.
        while time.monotonic() < deadline:
            timeout = "0" if "done" in statuses else "10"
            waited = self.captain("wait", "Jack", "--json", "--timeout", timeout, *ack)
            self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
            response = json.loads(waited.stdout)
            if response["delivery_id"]:
                statuses.append(response["status"])
                ack = ["--ack", response["delivery_id"]]
            elif "done" in statuses:
                break
        self.assertIn("done", statuses, statuses)
        dismissed = self.captain("dismiss", "Jack")
        self.assertEqual(dismissed.returncode, 0, dismissed.stdout + dismissed.stderr)
