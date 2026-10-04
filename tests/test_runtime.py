"""herdr's response contract: raw text, validated JSON, and no null nested values."""

import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase


class HerdrResponseTests(SessionCase):
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

    def test_rejects_outside_herdr_without_contacting_server(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(runtime.CaptainError, "Herdr workspace"):
                runtime.current_pane()
            api.assert_not_called()

    def test_pane_run_accepts_empty_stdout_when_output_is_not_expected(self):
        with (
            patch.object(runtime, "executable", return_value="/bin/herdr"),
            patch.object(
                runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            self.assertEqual(
                runtime.herdr("pane", "run", "w1:p2", "echo hello", expect_output=False), {}
            )

    def test_unexpected_herdr_response_names_command_and_includes_output(self):
        for stdout, options in (
            ("", {}),
            ("", {"expect_output": True}),
            ("not JSON\n", {"expect_output": False}),
            ('{"result": null}\n', {}),
            ("{}\n", {}),
            ("[]\n", {}),
            ('{"result": {"pane": null}}\n', {}),
            ('{"result": {"root_pane": "w1:p3"}}\n', {}),
            ('{"result": {"agent": ["builder"]}}\n', {}),
        ):
            with (
                self.subTest(stdout=stdout, options=options),
                patch.object(runtime, "executable", return_value="/bin/herdr"),
                patch.object(
                    runtime.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout, "CLI diagnostic"),
                ),
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "unexpected response") as error:
                    runtime.herdr("pane", "run", "w1:p2", "echo hello", **options)
                self.assertIn("herdr pane run", str(error.exception))
                self.assertIn(
                    f"Raw stdout:\n{stdout}\nStderr:\nCLI diagnostic", str(error.exception)
                )

    def herdr_stdout(self, responses):
        def run(command, **kwargs):
            payload = responses(tuple(command[1:]))
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": payload}), "")

        return run

    def test_null_nested_herdr_values_fail_as_captain_errors_in_every_caller(self):
        for response in (
            {"pane": None},
            {"pane": "w1:p1"},
            {"agent": None},
            {"agent": "x"},
            {"root_pane": []},
        ):
            with (
                self.subTest(response=response),
                patch.object(runtime, "executable", return_value="/bin/herdr"),
                patch.object(runtime.subprocess, "run", self.herdr_stdout(lambda _: response)),
                patch.object(panes.time, "sleep"),
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "unexpected response"):
                    runtime.current_pane()
                with self.assertRaisesRegex(runtime.CaptainError, "unexpected response"):
                    Pane("builder").wait_for_crew("w1:p2", "codex")
                with self.assertRaisesRegex(runtime.CaptainError, "unexpected response"):
                    Pane("builder").submit_task("build", "claude")
                with (
                    patch.object(cli, "project_root", return_value=self.project),
                    contextlib.redirect_stderr(io.StringIO()) as error,
                ):
                    self.assertEqual(cli.main(["--session", self.meta["id"], "focus", "Jack"]), 1)
                self.assertIn("captain: Herdr returned an unexpected response", error.getvalue())

    def test_null_nested_herdr_values_during_startup_preserve_the_pane(self):
        def responses(call):
            if call[:2] in (("pane", "split"), ("pane", "get")):
                return {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
            if call[:2] == ("agent", "get"):
                return {"agent": None}
            return {}

        args = self.args(
            "crew",
            "jack",
            "--agent",
            "codex",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        with (
            patch.object(runtime, "executable", return_value="/bin/herdr"),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(
                runtime.subprocess, "run", side_effect=self.herdr_stdout(responses)
            ) as run,
            patch.object(panes.time, "sleep"),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "pane was preserved") as error:
                agents.create_crew(args, self.pane, self.project)
        self.assertIn("unexpected response", str(error.exception))
        commands = [tuple(call.args[0][1:3]) for call in run.call_args_list]
        self.assertNotIn(("pane", "close"), commands)
        self.assertNotIn(("agent", "prompt"), commands)
        saved = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(saved["status"], "needs_attention")

    def test_herdr_returns_terminal_reads_as_raw_text(self):
        with (
            patch.object(runtime, "executable", return_value="/bin/herdr"),
            patch.object(
                runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "not JSON\n", ""),
            ),
        ):
            self.assertEqual(runtime.herdr("agent", "read", "builder", raw=True), "not JSON\n")


if __name__ == "__main__":
    unittest.main()
