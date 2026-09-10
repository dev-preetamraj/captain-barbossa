import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory
from captain_barbossa.runtime import CaptainError

CODEX_PICKER = """
Select Model and Effort

› 1. gpt-6-astra (current)  Our most capable model for complex, demanding work.
  2. gpt-5.6-sol            Reliable agentic workhorse for everyday tasks.
  3. gpt-5.6-terra          Balanced agentic coding model for everyday work.
  4. gpt-5.3-codex-spark    Ultra-fast coding model.

Press enter to confirm or esc to go back
"""
CODEX_EFFORT = """
Select Reasoning Level for gpt-5.6-terra

  1. Low               Fast responses with lighter reasoning
› 2. Medium (default)  Balances speed and reasoning depth for everyday tasks

Press enter to confirm or esc to go back
"""


class SwitchModelTests(unittest.TestCase):
    """Regression tests for mid-session model switching (agents.switch_model)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            )
        )
        self.enterContext(patch.object(agents.time, "sleep"))
        # Real deadlines: keep the polling loops short instead of waiting them out.
        self.enterContext(patch.object(agents, "MODEL_TIMEOUT", 0.05))
        self.enterContext(patch.object(agents, "PROMPT_TIMEOUT", 0.05))
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def crew(self, provider, model="claude-haiku-4-5"):
        agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": agent_name,
                "provider": provider,
                "pane": "w1:p2",
                "tab": "w1:t1",
                "model": model,
                "status": "started",
            }
        }
        memory.write_json(self.directory / "session.json", self.meta)
        return agent_name

    def switch(self, api, target):
        args = cli.parser().parse_args(["--session", self.meta["id"], "model", "Jack", target])
        with patch.object(agents, "herdr", side_effect=api) as herdr:
            agents.switch_model(args, self.pane, self.project)
        return herdr

    def reader(self, screens):
        """Answer pane reads from screens, advancing when the driven keys are sent."""
        state = {"screen": 0}

        def api(*args, **kwargs):
            if args[:2] == ("agent", "read"):
                return screens[min(state["screen"], len(screens) - 1)]
            if args[:2] in (("agent", "send-keys"), ("agent", "prompt")):
                state["screen"] += 1
            return {}

        return api

    def test_claude_switch_sends_the_slash_command_with_the_resolved_tier(self):
        agent_name = self.crew("claude")
        herdr = self.switch(
            self.reader(["thinking…", "⎿  Set model to Opus 5 and saved as your default"]),
            "strong",
        )
        self.assertIn(
            (("agent", "prompt", agent_name, "/model claude-opus-5"), {}),
            [(call.args, call.kwargs) for call in herdr.call_args_list],
        )
        self.assertIn("Jack switched to claude-opus-5.", self.output.getvalue())
        record = memory.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-opus-5")

    def test_a_switch_is_recorded_in_session_memory(self):
        self.crew("claude")
        self.switch(self.reader(["", "Set model to Sonnet 5 for this session only"]), "mid")
        graph = memory.read_json(self.directory / "graph.json")
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        recorded = [
            (labels[link["source"]], link["relation"], labels[link["target"]])
            for link in graph["links"]
        ]
        self.assertIn(("Jack", "model", "claude-sonnet-5"), recorded)

    def test_codex_switch_picks_the_numbered_row_and_keeps_the_default_effort(self):
        agent_name = self.crew("codex", "gpt-6-astra")
        herdr = self.switch(
            self.reader(
                ["", CODEX_PICKER, CODEX_EFFORT, "• Model changed to gpt-5.6-terra medium"]
            ),
            "mid",
        )
        keys = [call.args for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(
            keys,
            [("agent", "send-keys", agent_name, "3"), ("agent", "send-keys", agent_name, "enter")],
        )
        self.assertIn("Jack switched to gpt-5.6-terra.", self.output.getvalue())

    def test_a_pane_that_never_confirms_reports_an_error_and_records_nothing(self):
        self.crew("claude")
        with self.assertRaises(CaptainError) as error:
            self.switch(self.reader(["still working"]), "cheap")
        self.assertIn("did not confirm", str(error.exception))
        record = memory.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-haiku-4-5")

    def test_a_confirmation_naming_another_model_is_not_accepted(self):
        self.crew("claude")
        with self.assertRaises(CaptainError):
            self.switch(self.reader(["Set model to Haiku 4.5 for this session only"]), "strong")

    def test_codex_reports_a_model_its_picker_does_not_offer(self):
        self.crew("codex", "gpt-6-astra")
        with self.assertRaises(CaptainError) as error:
            self.switch(self.reader(["", CODEX_PICKER]), "gpt-5.5")
        self.assertIn("/model picker", str(error.exception))

    def test_an_unknown_crew_name_reports_the_available_crew(self):
        self.crew("claude")
        args = cli.parser().parse_args(["--session", self.meta["id"], "model", "Davy", "mid"])
        with self.assertRaises(CaptainError) as error:
            agents.switch_model(args, self.pane, self.project)
        self.assertIn("Jack", str(error.exception))


if __name__ == "__main__":
    unittest.main()
