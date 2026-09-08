import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, runtime


class LauncherCleanupTests(unittest.TestCase):
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
        self.enterContext(patch.object(agents, "READY_POLLS", 1))
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_dismiss_deletes_the_crew_launcher_script(self):
        created = {
            "root_pane": {"pane_id": "w1:p6"},
            "tab_id": "w1:t9",
            "pane": {
                "pane_id": "w1:p6",
                "tab_id": "w1:t9",
                "agent": "claude",
                "agent_status": "idle",
            },
            "agent": {"name": f"c-{self.meta['id'][:8]}-jack", "agent_status": "working"},
        }
        create_args = self.args(
            "crew", "jack", "--agent", "claude", "--task", "build", "--placement", "tab"
        )
        with (
            patch.object(agents, "herdr", side_effect=lambda *call, **_: created),
            patch.object(agents, "executable", return_value="/bin/claude"),
        ):
            agents.create_crew(create_args, self.pane, self.project)
        launcher = self.directory / "crew-jack.sh"
        self.assertTrue(launcher.exists())

        with patch.object(agents, "herdr", return_value={}):
            agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        self.assertFalse(launcher.exists())

    def test_failed_startup_deletes_the_crew_launcher_script(self):
        create_args = self.args(
            "crew", "jack", "--agent", "codex", "--task", "build", "--placement", "tab"
        )

        def api(*call, **_):
            if call[:2] == ("pane", "run"):
                raise runtime.CaptainError("agent_not_ready")
            return {"root_pane": {"pane_id": "w1:p6"}, "tab_id": "w1:t9"}

        with (
            patch.object(agents, "herdr", side_effect=api),
            patch.object(agents, "executable", return_value="/bin/codex"),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "pane was preserved"):
                agents.create_crew(create_args, self.pane, self.project)
        launcher = self.directory / "crew-jack.sh"
        self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
