import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, protocol, runtime
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane


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
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # These fakes don't model a realistic agent status for the mail doorbell; give
        # delivery a clean ring by default (test_submit.py covers nudge_block/nudge directly).
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
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

        # create_crew's shell_ready_for_input polls a raw `pane read`, which needs text;
        # only "agent read" carries the ambiguous "❯" composer check.
        def herdr_stub(*call, **_):
            if call[:2] == ("agent", "read"):
                return "❯"
            if call[:2] == ("pane", "read"):
                return "~/project $ "
            return created

        with (
            patch.object(runtime, "herdr", side_effect=herdr_stub),
            patch.object(agents, "executable", return_value="/bin/claude"),
        ):
            agents.create_crew(create_args, self.pane, self.project)
        launcher = self.directory / "crew-jack.sh"
        # A launcher that deleted itself the moment it ran made a failed startup
        # unrecoverable; it now survives, and dismiss_crew is what unlinks it.
        self.assertTrue(launcher.exists())
        self.assertNotIn('rm -f -- "$0"', launcher.read_text())

        current = memory.read_session(self.project, self.meta["id"], self.pane)
        crew = Crew.resolve(current, "Jack")
        protocol.change(
            crew,
            self.args(
                "done",
                "Jack",
                "--assignment",
                crew.record["assignment_id"],
                "--report",
                "Fixture complete; no files changed; no blockers",
            ),
            self.project,
        )
        delivery = protocol.poll(crew)
        protocol.poll(crew, delivery["delivery_id"])
        with patch.object(runtime, "herdr", return_value={}):
            agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
