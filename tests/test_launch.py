"""Launching the captain: the tab rename, the exec, and running outside the checkout."""

import contextlib
import io
import os
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions
from captain_barbossa import instructions as instruction_prompts
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase


class LaunchTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # nudge_block/nudge read a realistic agent status these ad hoc herdr fakes don't model;
        # a mail doorbell is not what these tests exercise, so give delivery a clean ring by
        # default. test_submit.py covers nudge_block/nudge themselves against real fakes.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_agent_commands_work_outside_the_source_checkout(self):
        instructions = instruction_prompts.agent_instructions(self.directory, "captain")
        command = next(
            line.strip() for line in instructions.splitlines() if " -m captain_barbossa " in line
        )
        launcher = shlex.split(command)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("HERDR_") and key != "PYTHONPATH"
        }
        help_result = subprocess.run(
            [*launcher, "--help"],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("create a native crew", help_result.stdout)
        guard_result = subprocess.run(
            launcher,
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(guard_result.returncode, 1)
        self.assertIn("Run captain inside a Herdr workspace", guard_result.stderr)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_launcher_renames_live_tab_and_executes_native_cli(self):
        for provider in ("claude", "codex"):
            with (
                self.subTest(provider=provider),
                patch.object(runtime, "herdr") as api,
                patch.object(agents, "executable", return_value=f"/bin/{provider}"),
                patch.object(os, "execvpe") as execute,
                patch.object(sys.stdin, "isatty", return_value=True),
                patch.object(agents, "captain_extension") as extension,
            ):
                args = self.args(
                    "--agent",
                    provider,
                    "--no-dashboard",
                    "--prompt",
                    "literal `touch /tmp/no` $(false)",
                )
                agents.launch(args, self.pane, self.project)
                api.assert_called_once_with("tab", "rename", "w1:t1", "Captain Barbossa")
                binary, argv, env = execute.call_args.args
                self.assertEqual(binary, f"/bin/{provider}")
                self.assertEqual(argv[-1], args.prompt)
                self.assertEqual(
                    argv[1:-2],
                    instruction_prompts.native_args(
                        provider,
                        instruction_prompts.agent_instructions(self.directory, "Captain Barbossa"),
                        events=self.directory / "events" / "captain.jsonl",
                    ),
                )
                self.assertEqual(env["CAPTAIN_SESSION"], self.meta["id"])
                self.assertEqual(env["CAPTAIN_PROJECT"], str(self.project))
                self.assertEqual(list(self.project.iterdir()), [])
                extension.assert_not_called()
                self.assertNotIn("--extension", argv)
                self.assertNotIn("captain_wait", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
