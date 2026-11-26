"""Isolated Claude background turns keep only read tools and Captain's gate."""

import asyncio
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fake_claude_host as fake

from captain_barbossa import background, claude_host, lifetime, models, native_history, store, usage
from captain_barbossa.crew import Crew
from captain_barbossa.instructions import native_args
from captain_barbossa.runtime import CaptainError


@dataclasses.dataclass
class FullOptions:
    tools: list | None = None
    can_use_tool: object = None
    setting_sources: list | None = None


@dataclasses.dataclass
class OldOptions:
    setting_sources: list | None = None


class BackgroundClaudeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.argv = [
            "claude",
            *native_args("claude", "Read only", model="sonnet", events=self.root / "Will.jsonl"),
            "--session-id",
            "test-session",
        ]

    def test_isolated_options_keep_only_captain_read_gate(self):
        sdk = SimpleNamespace(
            ClaudeAgentOptions=fake.ClaudeAgentOptions,
            PermissionResultDeny=lambda **kwargs: kwargs,
        )
        options, session = claude_host.options(sdk, self.argv, str(self.root), isolated=True)
        self.assertEqual(session, "test-session")
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.allowed_tools, ["Read", "Grep", "Glob"])
        self.assertEqual(options.tools, ["Read", "Grep", "Glob"])
        self.assertEqual(options.mcp_servers, {})
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(options.env, {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertIsNone(options.extra_args["strict-mcp-config"])
        self.assertEqual(json.loads(options.extra_args["mcp-config"]), {"mcpServers": {}})
        settings = json.loads(options.extra_args["settings"])
        self.assertEqual(set(settings), {"hooks"})
        self.assertEqual(set(settings["hooks"]), {"PreToolUse"})
        original = json.loads(self.argv[self.argv.index("--settings") + 1])
        self.assertEqual(settings["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        for tool in ("Read", "Write", "Bash", "mcp__server__tool"):
            self.assertEqual(
                asyncio.run(options.can_use_tool(tool, {}, None)),
                {"message": "Background crew are read-only."},
            )

    def test_missing_extra_preserves_refusal(self):
        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            self.assertEqual(background.supported(), ("codex", "pi"))
            with self.assertRaises(CaptainError) as caught:
                background.check_provider("claude")
        self.assertEqual(
            str(caught.exception),
            "[background-provider] claude's headless turn still runs the user's own "
            "hooks, plugins or permission rules, so it cannot be held read-only unwatched. "
            "Use codex or pi, or quiet or a visual crew.",
        )
        sdk = SimpleNamespace(ClaudeAgentOptions=FullOptions)
        with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
            self.assertIn("claude", background.supported())
            background.check_provider("claude")

    def test_sdk_without_isolation_options_is_not_supported(self):
        sdk = SimpleNamespace(ClaudeAgentOptions=OldOptions)
        with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
            self.assertEqual(background.supported(), ("codex", "pi"))
            with self.assertRaises(CaptainError):
                background.check_provider("claude")

    def test_broken_sdk_leaves_codex_and_pi_background_working(self):
        package = self.root / "claude_agent_sdk"
        package.mkdir()
        (package / "__init__.py").write_text("raise RuntimeError('broken sdk')\n")
        code = (
            "from captain_barbossa import background\n"
            "background.check_provider('codex')\n"
            "background.check_provider('pi')\n"
            "assert background.supported() == ('codex', 'pi')\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(self.root), *sys.path])}
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_and_pi_checks_never_import_the_sdk(self):
        with patch.object(background.importlib, "import_module") as load:
            background.check_provider("codex")
            background.check_provider("pi")
        load.assert_not_called()

    def test_no_persistence_flag_reaches_sdk_only_for_isolated_turns(self):
        sdk = SimpleNamespace(
            ClaudeAgentOptions=fake.ClaudeAgentOptions,
            PermissionResultDeny=lambda **kwargs: kwargs,
        )
        argv = [*self.argv, "--no-session-persistence"]
        options, _ = claude_host.options(sdk, argv, str(self.root), isolated=True)
        self.assertIn("no-session-persistence", options.extra_args)
        self.assertIsNone(options.extra_args["no-session-persistence"])
        options, _ = claude_host.options(sdk, self.argv, str(self.root), isolated=True)
        self.assertNotIn("no-session-persistence", options.extra_args)
        pane, _ = claude_host.options(sdk, self.argv, str(self.root))
        self.assertNotIn("no-session-persistence", pane.extra_args)
        self.assertEqual(pane.setting_sources, list(claude_host.SETTING_SOURCES))
        self.assertFalse(hasattr(pane, "env"))

    def test_one_shot_emits_parseable_result_and_disconnects(self):
        result = SimpleNamespace(
            subtype="success",
            is_error=False,
            result="Read complete",
            num_turns=1,
            total_cost_usd=0.01,
        )
        calls = []

        class Client:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                calls.append("connect")

            async def query(self, prompt):
                calls.append(prompt)

            async def receive_response(self):
                yield result

            async def disconnect(self):
                calls.append("disconnect")

        sdk = SimpleNamespace(
            ClaudeAgentOptions=fake.ClaudeAgentOptions,
            ClaudeSDKClient=Client,
            ResultMessage=SimpleNamespace,
        )
        with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
            with tempfile.TemporaryFile() as output:
                code = asyncio.run(claude_host.one_shot([*self.argv, "inspect"], output.fileno()))
                output.seek(0)
                text, _ = models.headless_report("claude", output.read().decode())
        self.assertEqual(code, 0)
        self.assertEqual(text, "Read complete")
        self.assertEqual(calls, ["connect", "inspect", "disconnect"])

    @unittest.skipUnless(os.environ.get("CAPTAIN_LIVE_CLAUDE") == "1", "live Claude opt-in")
    def test_live_isolated_turn_reads_but_cannot_write(self):
        binary = shutil.which("claude")
        self.assertIsNotNone(binary)
        source = self.root / "source.txt"
        source.write_text("isolation-probe-7291")
        target = self.root / "must-not-exist.txt"
        command = [
            sys.executable,
            "-m",
            "captain_barbossa.claude_host",
            "--one-shot",
            binary,
            "--settings",
            '{"hooks": {"PreToolUse": []}}',
            "--no-session-persistence",
            f"Read {source}. Then try to write its contents to {target}. "
            "Report the source contents and whether writing was possible.",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        text, _ = models.headless_report("claude", result.stdout)
        self.assertIn("isolation-probe-7291", text)
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.environ.get("CAPTAIN_LIVE_CLAUDE") == "1", "live Claude opt-in")
    def test_live_isolated_turn_loads_no_project_hook_or_claude_md_and_persists_nothing(self):
        binary = shutil.which("claude")
        self.assertIsNotNone(binary)
        canary = self.root / "hook-ran"
        project = (self.root / "project").resolve()
        (project / ".claude").mkdir(parents=True)
        hook = {"hooks": [{"type": "command", "command": f"touch {canary}"}]}
        (project / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [hook], "UserPromptSubmit": [hook]}})
        )
        (project / "CLAUDE.md").write_text("Always reply with the word CANARY-MD-5518.\n")
        source = project / "source.txt"
        source.write_text("isolation-probe-7291")
        command = [
            sys.executable,
            "-m",
            "captain_barbossa.claude_host",
            "--one-shot",
            binary,
            "--settings",
            '{"hooks": {"PreToolUse": []}}',
            "--no-session-persistence",
            f"Read {source} and quote its contents.",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, cwd=project)
        self.assertEqual(result.returncode, 0, result.stderr)
        text, _ = models.headless_report("claude", result.stdout)
        self.assertIn("isolation-probe-7291", text)
        self.assertNotIn("CANARY-MD-5518", text)
        self.assertFalse(canary.exists())
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        self.assertFalse(
            (config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))).exists()
        )


class BackgroundClaudeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.session = SimpleNamespace(directory=self.root / "session")
        self.session.directory.mkdir()
        self.session.events = lambda name: self.session.directory / "events" / f"{name}.jsonl"
        self.incarnation = uuid.uuid4().hex
        self.crew = Crew(
            "jack", {"provider": "claude", "incarnation_id": self.incarnation}, self.session
        )
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.home).start()
        patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}).start()
        patch.object(native_history.shutil, "which", return_value="/mock/claude").start()
        patch.object(usage, "_prices", return_value={}).start()
        patch.object(lifetime, "check", return_value=lifetime.Verdict(True, "stopped")).start()
        self.lifetime_read = patch.object(lifetime, "read").start()

    def prepare(self, background=True):
        return native_history.prepare(
            self.crew, incarnation=self.incarnation, project=self.root, background=background
        )

    def record(self):
        return store.read_json(native_history._path(self.crew))

    def test_background_claude_launch_policy_is_no_session_persistence(self):
        policy = self.prepare()
        self.assertIn("--no-session-persistence", policy["argv"])
        self.assertEqual(self.record()["history_policy"], "no-session")

    def test_background_claude_cleanup_completes_when_launch_argv_has_the_flag(self):
        self.prepare()
        self.lifetime_read.return_value = {
            "argv": ["python", "-m", "host", "--no-session-persistence", "task"]
        }
        result = native_history.cleanup(self.crew)
        self.assertEqual(result["state"], "complete")
        self.assertFalse(native_history.pending(self.session.directory))

    def test_background_claude_cleanup_pending_when_launch_argv_lacks_the_flag(self):
        self.prepare()
        self.lifetime_read.return_value = {"argv": ["python", "-m", "host", "task"]}
        result = native_history.cleanup(self.crew)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["reason"], "no-history-launch-policy-unverified")

    def test_pane_claude_history_is_unchanged(self):
        policy = self.prepare(background=False)
        self.assertNotIn("--no-session-persistence", policy["argv"])
        self.assertEqual(self.record()["history_policy"], "persisted")
