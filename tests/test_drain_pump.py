import contextlib
import io
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import cli, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import DRAFT_EXPIRY, DRAFT_GATE, Pane
from captain_barbossa.runtime import CaptainError
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

    def test_registration_recovers_without_replaying_the_task(self):
        with patch.object(protocol.time, "time", return_value=1000) as clock:
            self.nudge_block.side_effect = CaptainError("Herdr: agent_not_found")
            message_id = protocol.deliver(self.crew, "original", initial=True)
            self.assertEqual([m["id"] for m in protocol.unread(self.crew)], [message_id])
            self.assertEqual(protocol.last_ring(self.crew)["gate"], "awaiting registration")
            self.nudge_block.reset_mock()
            self.assertEqual(self.captain_command()[0], 0)
            self.nudge_block.assert_not_called()  # rate limited even during startup
            clock.return_value += protocol.DRAIN_INTERVAL
            self.assertEqual(self.captain_command()[0], 0)
            self.nudge.assert_not_called()
            self.nudge_block.side_effect = None
            clock.return_value += protocol.DRAIN_INTERVAL
            self.assertEqual(self.captain_command()[0], 0)
            self.nudge.assert_called_once()
            self.assertIn("original", self.nudge.call_args.args[1])
            protocol.deliver(self.crew, "original", initial=True)
            self.captain_command()
            self.nudge.assert_called_once()
            clock.return_value += protocol.DRAIN_LANDED_INTERVAL
            self.captain_command()
            self.assertEqual(self.nudge.call_count, 2)
            self.assertIsNone(self.nudge.call_args.args[1])  # wake only, never the task again
            protocol.receipt(self.current.directory, "jack", [message_id])
            clock.return_value += protocol.DRAIN_LANDED_INTERVAL
            self.captain_command()
            self.assertEqual(self.nudge.call_count, 2)

    def test_missing_registration_expires_without_resetting_its_budget(self):
        with patch.object(protocol.time, "time", return_value=1000) as clock:
            self.nudge_block.side_effect = CaptainError("jack is not registered.")
            message_id = protocol.deliver(self.crew, "original", initial=True)
            clock.return_value += protocol.REGISTRATION_GRACE - 1
            self.captain_command()
            self.assertEqual(len(protocol.unread(self.crew)), 1)
            clock.return_value += protocol.DRAIN_INTERVAL
            self.captain_command()
            self.assertEqual(protocol.unread(self.crew), [])
            path = protocol.mail_dir(self.current.directory, "jack") / f"{message_id}.json"
            self.assertEqual(store.read_json(path)["state"], "bounced")
            self.assertIn("not registered", store.read_json(path)["reason"])
            self.nudge_block.side_effect = None
            clock.return_value += protocol.DRAIN_INTERVAL
            self.captain_command()
            protocol.deliver(self.crew, "original", initial=True)
            self.nudge.assert_not_called()  # terminal mail is never resurrected

    def test_permanent_startup_failure_still_bounces_immediately(self):
        self.nudge_block.side_effect = CaptainError("Herdr: pane_not_found")
        message_id = protocol.deliver(self.crew, "original", initial=True)
        self.assertEqual(protocol.unread(self.crew), [])
        path = protocol.mail_dir(self.current.directory, "jack") / f"{message_id}.json"
        self.assertEqual(store.read_json(path)["state"], "bounced")
        self.nudge_block.side_effect = None
        self.captain_command()
        self.nudge.assert_not_called()

    def test_missing_registration_does_not_retry_followups(self):
        self.nudge_block.side_effect = CaptainError("Herdr: agent_not_found")
        protocol.deliver(self.crew, "followup")
        self.assertEqual(protocol.unread(self.crew), [])
        self.nudge.assert_not_called()

    def test_missing_registration_after_landing_is_not_startup(self):
        with patch.object(protocol.time, "time", return_value=1000):
            message_id = protocol.deliver(self.crew, "original", initial=True)
            self.nudge_block.side_effect = CaptainError("Herdr: agent_not_found")
            protocol.ring(self.crew, message_id)
            self.assertEqual(protocol.unread(self.crew), [])
            self.nudge.assert_called_once()

    def test_receipt_during_registration_wait_stops_retries(self):
        with patch.object(protocol.time, "time", return_value=1000) as clock:
            self.nudge_block.side_effect = CaptainError("Herdr: agent_not_found")
            message_id = protocol.deliver(self.crew, "original", initial=True)
            protocol.receipt(self.current.directory, "jack", [message_id])
            self.nudge_block.side_effect = None
            clock.return_value += protocol.DRAIN_INTERVAL
            self.captain_command()
            self.nudge.assert_not_called()

    def test_stale_incarnation_mail_gets_no_registration_grace(self):
        message_id = protocol.deliver(self.crew, "original", initial=True, queue_only=True)
        self.crew.record["incarnation_id"] = "replacement"
        self.nudge_block.side_effect = CaptainError("Herdr: agent_not_found")
        protocol.ring(self.crew, message_id)
        self.assertEqual(protocol.unread(self.crew), [])
        self.nudge.assert_not_called()

    def wait_command(self, *options, timeout=0, all_crew=False):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(
                ["--session", self.session_id, "wait"]
                + (["--all"] if all_crew else ["Jack"])
                + ["--json", "--timeout", str(timeout), *options]
            )
        return code, json.loads(output.getvalue())

    def missing_registration(self):
        error = CaptainError("Herdr: agent_not_found")
        self.nudge_block.side_effect = error
        self.lines = self.enterContext(patch.object(Pane, "lines", side_effect=error))
        return protocol.deliver(self.crew, "original", initial=True)

    def native_event(self, kind):
        self.crew.events.parent.mkdir(exist_ok=True)
        with self.crew.events.open("a") as stream:
            stream.write(json.dumps({"hook_event_name": kind}) + "\n")

    def test_cli_wait_missing_registration_times_out_with_assignment(self):
        message_id = self.missing_registration()
        code, result = self.wait_command()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["assignment_id"], self.assignment)
        self.assertIsNone(result["delivery_id"])
        self.assertEqual(protocol.unread(self.crew)[0]["id"], message_id)
        self.assertGreaterEqual(self.lines.call_count, 2)  # real choice_modal and summary
        code, result = self.wait_command(all_crew=True)
        self.assertEqual(code, 0)
        self.assertEqual(result, {"status": "timeout", "deliveries": []})

    def test_same_cli_wait_recovers_registration_and_delivers_once(self):
        with (
            patch.object(protocol.time, "time", return_value=1000) as clock,
            patch.object(protocol.time, "monotonic", return_value=1000) as monotonic,
        ):
            message_id = self.missing_registration()

            def tick(seconds):
                clock.return_value += seconds
                monotonic.return_value += seconds
                if clock.return_value >= 1000 + protocol.DRAIN_INTERVAL:
                    self.nudge_block.side_effect = None
                    self.lines.side_effect = None
                    self.lines.return_value = []

            def delivered(*args):
                protocol.receipt(self.current.directory, "jack", [message_id])
                self.native_event("Stop")

            self.nudge.side_effect = delivered
            with patch.object(protocol.time, "sleep", side_effect=tick):
                code, result = self.wait_command(timeout=protocol.DRAIN_INTERVAL + 1)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "idle")
            self.assertEqual(result["assignment_id"], self.assignment)
            self.assertTrue(result["delivery_id"])
            self.nudge.assert_called_once()
            self.assertIn("original", self.nudge.call_args.args[1])
            self.assertEqual(protocol.unread(self.crew), [])

    def test_cli_wait_expiry_surfaces_acknowledgeable_bounce_at_exact_boundary(self):
        with patch.object(protocol.time, "time", return_value=1000) as clock:
            self.missing_registration()
            clock.return_value += protocol.REGISTRATION_GRACE - 1
            self.assertEqual(self.wait_command()[1]["status"], "timeout")
            clock.return_value += 1  # expiry must not borrow another cooldown
            code, result = self.wait_command()
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "bounced")
            self.assertEqual(result["assignment_id"], self.assignment)
            self.assertTrue(result["delivery_id"])
            self.assertEqual(self.wait_command()[1], result)
            self.lines.side_effect = None
            self.lines.return_value = []
            code, acknowledged = self.wait_command("--ack", result["delivery_id"])
            self.assertEqual(code, 0)
            self.assertEqual(acknowledged["status"], "timeout")
            self.assertEqual(protocol.unread(self.crew), [])

    def test_cli_wait_preserves_genuine_modal_during_registration_grace(self):
        self.missing_registration()
        self.lines.side_effect = None
        self.lines.return_value = [
            "Run git push origin main?",
            "1. Allow",
            "2. Deny",
            "Press enter to confirm or esc to cancel",
        ]
        code, result = self.wait_command()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertIn("git push", result["summary"])
        self.assertTrue(result["delivery_id"])

    def test_cli_wait_does_not_hide_permanent_modal_read_failure(self):
        self.missing_registration()
        self.lines.side_effect = CaptainError("Herdr: pane_not_found")
        code, result = self.wait_command()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("pane_not_found", result["summary"])

    def test_cli_wait_handles_native_approval_and_pending_delivery_before_pane(self):
        self.missing_registration()
        self.native_event("PermissionRequest")
        code, result = self.wait_command()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertTrue(result["delivery_id"])
        self.assertEqual(self.wait_command()[1], result)
        self.lines.assert_not_called()

    def test_registration_grace_requires_current_assignment_identity_and_no_turn(self):
        message_id = self.missing_registration()
        original = protocol.active_assignment(self.crew)
        variants = [
            {**original, "incarnation_id": "other"},
            {**original, "id": "replacement"},
            {**original, "turn_started": True},
        ]
        for assignment in variants:
            with self.subTest(assignment=assignment["id"], turn=assignment.get("turn_started")):
                self.assertFalse(
                    protocol.awaiting_registration(
                        self.crew,
                        message_id,
                        protocol.last_ring(self.crew),
                        CaptainError("agent_not_found"),
                        assignment,
                    )
                )

    def test_unpolled_native_turn_denies_registration_grace_but_session_start_does_not(self):
        message_id = self.missing_registration()
        self.native_event("SessionStart")
        error = CaptainError("agent_not_found")
        self.assertTrue(protocol.awaiting_registration(self.crew, message_id, None, error))
        self.native_event("PostToolUse")
        self.assertFalse(protocol.active_assignment(self.crew).get("turn_started"))
        self.assertFalse(protocol.awaiting_registration(self.crew, message_id, None, error))
        protocol.ring(self.crew, message_id)
        self.assertEqual(protocol.unread(self.crew), [])

    def test_cli_wait_receipted_mail_does_not_suppress_missing_registration(self):
        message_id = self.missing_registration()
        protocol.receipt(self.current.directory, "jack", [message_id])
        code, result = self.wait_command()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("agent_not_found", result["summary"])

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
