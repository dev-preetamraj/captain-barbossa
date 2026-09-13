import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory


class CrewTabLabelTests(unittest.TestCase):
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
        for name in ("CAPTAIN_STATE_ROOT", "CAPTAIN_TEMP_ROOT"):
            os.environ.pop(name, None)
        self.enterContext(patch.object(agents, "READY_POLLS", 1))
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_tab_label_shows_all_crew_occupants(self):
        """Tab labels update on second crew join and on dismissal."""
        created_jack = {
            "pane": {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
            "root_pane": {"pane_id": "w1:p2"},
            "tab_id": "w1:t9",
            "agent": {"name": f"c-{self.meta['id'][:8]}-jack", "agent_status": "working"},
        }
        created_will = {
            "pane": {"pane_id": "w1:p3", "agent": "claude", "agent_status": "idle"},
            "root_pane": {"pane_id": "w1:p3"},
            "tab_id": "w1:t9",
            "agent": {"name": f"c-{self.meta['id'][:8]}-will", "agent_status": "working"},
        }

        args_jack = self.args(
            "crew",
            "jack",
            "--agent",
            "claude",
            "--task",
            "build",
            "--placement",
            "tab",
        )
        with (
            patch.object(agents, "herdr", return_value=created_jack),
            patch.object(agents, "executable", return_value="/bin/claude"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.create_crew(args_jack, self.pane, self.project)

        herdr_calls = []

        def track_herdr(*call, **_):
            herdr_calls.append(call)
            if call[:2] == ("tab", "rename"):
                return {}
            return created_will

        args_will = self.args(
            "crew", "will", "--agent", "claude", "--task", "review", "--placement", "tab"
        )
        with (
            patch.object(agents, "herdr", side_effect=track_herdr),
            patch.object(agents, "executable", return_value="/bin/claude"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.create_crew(args_will, self.pane, self.project)

        rename_calls = [c for c in herdr_calls if c[:2] == ("tab", "rename")]
        self.assertEqual(len(rename_calls), 1, "Should rename tab once when second crew joins")
        self.assertEqual(rename_calls[0][2], "w1:t9", "Should rename the crew tab")
        self.assertEqual(rename_calls[0][3], "Jack +1", "Label should show first crew and +1")

        herdr_calls.clear()

        def track_dismiss(*call, **_):
            herdr_calls.append(call)
            return {}

        args_dismiss = self.args("dismiss", "jack")
        with (
            patch.object(agents, "herdr", side_effect=track_dismiss),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.dismiss_crew(args_dismiss, self.pane, self.project)

        rename_calls = [c for c in herdr_calls if c[:2] == ("tab", "rename")]
        self.assertEqual(len(rename_calls), 1, "Should rename tab when crew dismissed")
        self.assertEqual(rename_calls[0][2], "w1:t9", "Should rename the crew tab")
        self.assertEqual(rename_calls[0][3], "Will", "Label should show only remaining crew")
