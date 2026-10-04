import contextlib
import io
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import cli, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import DRAFT_EXPIRY, DRAFT_GATE, Pane
from tests.home_isolation import SessionCase


class DrainPumpTests(SessionCase):
    """Regression: captain activity retries a doorbell held for an idle crew, outside any wait."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.session_id = self.current.meta["id"]
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "incarnation_id": "first",
                "pane": "w1:p2",
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.object(cli, "current_pane", return_value=self.pane))
        self.nudge_block = self.enterContext(
            patch.object(Pane, "nudge_block", return_value=None, create=True)
        )
        self.nudge = self.enterContext(patch.object(Pane, "nudge", create=True))
        # A reported draft gate makes ring read the composer to age it; these fakes do not
        # model a screen, and an unpatched read would reach for the real herdr.
        self.draft = self.enterContext(patch.object(Pane, "draft", return_value="held draft"))
        assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])
        self.assignment = assignment["id"]

    def hold_mail(self, *, age=60):
        """Queue mail whose doorbell was held behind a draft gate `age` seconds ago."""
        self.nudge_block.return_value = DRAFT_GATE
        message_id = protocol.deliver(self.crew, "original", initial=True)
        self.nudge_block.return_value = None
        stamp = protocol.drain_stamp(self.crew)
        record = store.read_json(stamp)
        record["at"] = record["held_since"] = time.time() - age
        store.write_json(stamp, record)
        self.nudge.reset_mock()
        self.nudge_block.reset_mock()
        return message_id

    def captain_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(
                [
                    "--session",
                    self.session_id,
                    "check",
                    "Jack",
                    "--assignment",
                    self.assignment,
                    "read",
                    "src",
                ]
            )
        return code, output.getvalue()

    def test_ordinary_command_retries_a_held_doorbell(self):
        self.hold_mail()
        code, output = self.captain_command()
        self.assertEqual(code, 0)
        self.assertIn('"allowed": true', output)
        self.nudge.assert_called_once()

    def test_a_draft_stops_holding_once_it_outlives_the_expiry(self):
        """One unchanged draft holds mail only up to DRAFT_EXPIRY, and never fuses the body."""
        self.hold_mail()
        self.nudge_block.return_value = DRAFT_GATE
        stamp = protocol.drain_stamp(self.crew)
        code, _ = self.captain_command()
        self.assertEqual(code, 0)
        self.nudge.assert_not_called()  # a draft just seen still holds
        record = store.read_json(stamp)
        self.assertEqual(record["draft"], "held draft")
        record["at"] = record["draft_since"] = time.time() - DRAFT_EXPIRY - 1
        store.write_json(stamp, record)
        code, _ = self.captain_command()
        self.assertEqual(code, 0)
        self.nudge.assert_called_once()
        # Expired means ringable, not fusable: the inbox line alone, never the mail body.
        self.assertIsNone(self.nudge.call_args.args[1])
        self.assertTrue(store.read_json(stamp)["landed"])

    def test_an_edited_draft_starts_the_expiry_clock_over(self):
        self.hold_mail()
        self.nudge_block.return_value = DRAFT_GATE
        stamp = protocol.drain_stamp(self.crew)
        record = store.read_json(stamp)
        record["at"] = record["draft_since"] = time.time() - DRAFT_EXPIRY - 1
        record["draft"] = "what the human typed before"
        store.write_json(stamp, record)
        code, _ = self.captain_command()
        self.assertEqual(code, 0)
        self.nudge.assert_not_called()
        self.assertGreater(store.read_json(stamp)["draft_since"], record["draft_since"])

    def test_no_unread_mail_rings_nothing(self):
        code, _ = self.captain_command()
        self.assertEqual(code, 0)
        self.nudge_block.assert_not_called()
        self.nudge.assert_not_called()

    def test_drain_failure_does_not_fail_the_command(self):
        self.hold_mail()
        with patch.object(cli.protocol, "drain", side_effect=RuntimeError("boom")):
            code, output = self.captain_command()
        self.assertEqual(code, 0)
        self.assertIn('"allowed": true', output)

    def test_crew_role_pumps_nothing(self):
        self.hold_mail()
        args = SimpleNamespace(session=self.session_id)
        with (
            patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}),
            patch.object(cli.protocol, "drain") as drain,
        ):
            cli.pump_mail(args, self.pane, self.project)
        drain.assert_not_called()

    def test_dismissed_crew_is_skipped(self):
        self.hold_mail()
        self.crew.record["status"] = "dismissed"
        store.write_json(self.current.meta_path, self.current.meta)
        with patch.object(cli.protocol, "drain") as drain:
            cli.pump_mail(SimpleNamespace(session=self.session_id), self.pane, self.project)
        drain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
