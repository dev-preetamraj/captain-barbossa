"""claude_host turns a pane crew's claude argv into Agent SDK options, with the SDK optional."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fake_claude_host
import home_isolation  # noqa: F401

from captain_barbossa import claude_host, instructions
from captain_barbossa.runtime import CaptainError


class ClaudeHostOptionsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="claude-host-"))
        self.events = self.root / "events" / "jack-abc.jsonl"

    def test_pane_crew_argv_becomes_sdk_options_without_a_permission_callback(self):
        flags = instructions.native_args(
            "claude", "You are crew member Jack.", "claude-sonnet-5-5", self.events
        )
        argv = ["/opt/bin/claude", *flags, "--session-id", "sid-1"]
        built, session_id = claude_host.options(fake_claude_host, argv, "/work")
        self.assertEqual(session_id, "sid-1")
        self.assertEqual(
            built.given,
            {
                "cli_path": "/opt/bin/claude",
                "system_prompt": {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": "You are crew member Jack.",
                },
                "permission_mode": "auto",
                "extra_args": {
                    "settings": flags[flags.index("--settings") + 1],
                    "model": "claude-sonnet-5-5",
                    "session-id": "sid-1",
                },
                "setting_sources": ["user", "project", "local"],
                "cwd": "/work",
            },
        )
        hooks = json.loads(built.extra_args["settings"])["hooks"]
        self.assertIn("PreToolUse", hooks)
        self.assertIn("PermissionRequest", hooks)

    def test_a_malformed_argv_is_refused(self):
        for argv in (["/c", "--a"], ["/c", "x", "y"], ["/c", "--a", "1", "--a", "2"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(CaptainError, r"\[claude-host\]"):
                claude_host.options(fake_claude_host, argv, "/work")

    def test_the_sdk_transport_runs_the_user_claude_under_the_host(self):
        python = instructions.python_command()
        self.assertEqual(
            instructions.native_command("/opt/bin/claude", "sdk"),
            [python, "-m", "captain_barbossa.claude_host", "/opt/bin/claude"],
        )
        self.assertEqual(instructions.native_command("/opt/bin/claude"), ["/opt/bin/claude"])
        self.assertEqual(
            instructions.native_command("/opt/bin/codex", "app-server"),
            ["/opt/bin/codex", "app-server"],
        )

    def test_a_missing_sdk_refuses_the_connect_with_the_install_hint(self):
        host = claude_host.Host(["/opt/bin/claude"], 1)
        with (
            patch.dict(sys.modules, {"claude_agent_sdk": None}),
            self.assertRaisesRegex(CaptainError, r"\[claude-sdk-missing\].*\[app\]"),
        ):
            asyncio.run(host.connect())

    def test_claude_closed_by_us_exits_clean_and_its_own_exit_keeps_its_code(self):
        host = claude_host.Host(["/opt/bin/claude"], 1)
        host.client = object()
        for code, expected in ((0, 0), (143, 0), (-15, 0), (-9, 1), (2, 2), (None, 1)):
            with self.subTest(code=code):
                host.process = SimpleNamespace(returncode=code)
                self.assertEqual(host.returncode(), expected)
        host.exited = 3
        self.assertEqual(host.returncode(), 3)

    def test_herdr_commands_and_hooks_never_load_the_claude_host_or_sdk(self):
        code = (
            "import sys; import captain_barbossa.cli, captain_barbossa.events; "
            "print(sorted(m for m in sys.modules if m.rsplit('.', 1)[-1] in "
            "('claude_agent', 'claude_host', 'claude_agent_sdk')))"
        )
        loaded = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
        )
        self.assertEqual(loaded.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
