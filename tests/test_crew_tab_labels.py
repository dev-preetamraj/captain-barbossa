import contextlib
import io
import os
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, runtime, sessions
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase


class CrewTabLabelTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # These fakes don't model a realistic agent status for the mail doorbell; give
        # delivery a clean ring by default (test_submit.py covers nudge_block/nudge directly).
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
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
            patch.object(
                runtime,
                "herdr",
                side_effect=lambda *call, **_: (
                    "❯"
                    if call[:2] == ("agent", "read")
                    else "~/project $ "
                    if call[:2] == ("pane", "read")
                    else created_jack
                ),
            ),
            patch.object(agents, "executable", return_value="/bin/claude"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.create_crew(args_jack, self.pane, self.project)

        herdr_calls = []

        def track_herdr(*call, **_):
            herdr_calls.append(call)
            if call[:2] == ("agent", "read"):
                return "❯"
            if call[:2] == ("pane", "read"):
                return "~/project $ "
            if call[:2] == ("tab", "rename"):
                return {}
            return created_will

        args_will = self.args(
            "crew", "will", "--agent", "claude", "--task", "review", "--placement", "tab"
        )
        with (
            patch.object(runtime, "herdr", side_effect=track_herdr),
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
        current = sessions.read_session(self.project, self.meta["id"], self.pane)
        crew = Crew.resolve(current, "Jack")
        unread_messages = protocol.unread(crew)
        protocol.receipt(
            crew.session.directory, crew.crew_id, [msg["id"] for msg in unread_messages]
        )
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
        with (
            patch.object(runtime, "herdr", side_effect=track_dismiss),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.dismiss_crew(args_dismiss, self.pane, self.project)

        rename_calls = [c for c in herdr_calls if c[:2] == ("tab", "rename")]
        self.assertEqual(len(rename_calls), 1, "Should rename tab when crew dismissed")
        self.assertEqual(rename_calls[0][2], "w1:t9", "Should rename the crew tab")
        self.assertEqual(rename_calls[0][3], "Will", "Label should show only remaining crew")
