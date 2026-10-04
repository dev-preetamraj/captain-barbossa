import contextlib
import io
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import DRAFT_GATE, UNREADABLE_EXPIRY, UNREADABLE_GATE, Pane
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase

# ProtocolTests.setUp stubs nudge_block, so a test of the gate itself restores this.
REAL_NUDGE_BLOCK = Pane.nudge_block


class ProtocolTests(SessionCase):
    def setUp(self):
        # Not super(): the classes below borrow this setUp with an unbound call.
        SessionCase.setUp(self)
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.directory = self.current.directory
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "agent_status", return_value=None))
        self.enterContext(patch.object(Pane, "choice_modal", return_value=False))
        # nudge/nudge_block are pane.py's, built concurrently; stub the doorbell for delivery.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None, create=True))
        self.enterContext(patch.object(Pane, "nudge", create=True))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def mail(self, message_id):
        crew_id, session = self.crew.crew_id, self.current
        return store.read_json(protocol.mail_dir(session.directory, crew_id) / f"{message_id}.json")

    def rendered(self, message_id):
        """What a crew without a delivery hook reads off its ring: identity plus body."""
        return protocol.render(self.current.directory, [self.mail(message_id)])

    def saved(self):
        return store.read_json(self.directory / "protocol.json")["assignments"][
            self.assignment["id"]
        ]

    def command(self, command, **values):
        args = SimpleNamespace(
            command=command, assignment=self.assignment["id"], incarnation="first", **values
        )
        return protocol.change(self.crew, args, self.project)

    def event(self, event):
        self.crew.events.parent.mkdir(exist_ok=True)
        with self.crew.events.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def finish(self, report="src/a.py changed; checks pass; nothing left"):
        """Done with its notification acknowledged, which is what dismissal requires."""
        self.command("done", report=report)
        protocol.poll(self.crew, protocol.poll(self.crew)["delivery_id"])

    def test_persist_before_single_send_and_preserve_original_task(self):
        first = protocol.deliver(self.crew, "original", initial=True)
        second = protocol.deliver(self.crew, "followup")
        saved = self.saved()
        self.assertEqual(saved["original_task"], "original")
        self.assertEqual([m["text"] for m in saved["messages"]], ["original", "followup"])
        self.assertEqual([m["delivery"] for m in saved["messages"]], ["sent", "sent"])
        self.assertEqual(len({m["id"] for m in saved["messages"]}), 2)
        for message_id, text in ((first, "original"), (second, "followup")):
            mail = self.mail(message_id)
            self.assertEqual(mail["state"], "queued")
            # The body is the captain's text alone; identity renders at read time.
            self.assertEqual(mail["text"], text)
        header = protocol.identity(saved)
        self.assertTrue(header.startswith(f"Crew name: Jack.\nAssignment {saved['id']}; "))

    def test_a_failed_enqueue_leaves_no_sent_message_and_allows_retry(self):
        real_write_json = protocol.write_json
        mail_directory = protocol.mail_dir(self.directory, self.crew.crew_id)

        def flaky(path, data):
            if path.is_relative_to(mail_directory):
                raise OSError("disk full")
            real_write_json(path, data)

        with patch.object(protocol, "write_json", side_effect=flaky):
            with self.assertRaises(OSError):
                protocol.deliver(self.crew, "original", initial=True)
        self.assertEqual(self.saved()["messages"], [])
        self.assertEqual(protocol.unread(self.crew), [])
        message_id = protocol.deliver(self.crew, "original", initial=True)
        self.assertEqual(self.mail(message_id)["state"], "queued")
        self.assertEqual([m["text"] for m in self.saved()["messages"]], ["original"])

    def test_a_refused_ring_never_loses_the_mail(self):
        with (
            patch.object(Pane, "nudge_block", return_value="cooldown"),
            patch.object(Pane, "nudge") as nudge,
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        nudge.assert_not_called()
        self.assertEqual(self.saved()["messages"][0]["delivery"], "sent")
        self.assertEqual(self.mail(message_id)["state"], "queued")

    def test_a_dead_pane_bounces_the_mail_instead_of_losing_it(self):
        with patch.object(Pane, "nudge", side_effect=CaptainError("pane not found")):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        self.assertEqual(self.saved()["messages"][0]["delivery"], "sent")
        mail = self.mail(message_id)
        self.assertEqual(mail["state"], "bounced")
        self.assertIn("pane not found", mail["reason"])
        response = protocol.poll(self.crew)
        self.assertEqual(response["status"], "bounced")
        self.assertIn(message_id, response["summary"])

    def test_ring_holds_on_a_broken_doorbell_without_bouncing(self):
        with patch.object(Pane, "nudge", side_effect=AttributeError("no nudge yet")):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        self.assertEqual(self.saved()["messages"][0]["delivery"], "sent")
        self.assertEqual(self.mail(message_id)["state"], "queued")

    def test_poll_drains_held_mail_at_most_once_per_cooldown(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value="cooldown"),
            patch.object(Pane, "nudge") as nudge,
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
            nudge.assert_not_called()
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=None),
            patch.object(Pane, "nudge") as nudge,
        ):
            protocol.poll(self.crew)
            nudge.assert_not_called()
            clock[0] += protocol.DRAIN_INTERVAL + 1
            protocol.poll(self.crew)
            protocol.poll(self.crew)
            nudge.assert_called_once_with(self.crew, self.rendered(message_id))

    def test_a_landed_ring_is_not_repeated_by_the_next_poll(self):
        """The delivery ring already landed, so a wait a second later must not nudge again."""
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge") as nudge,
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
            body = self.rendered(message_id)
            nudge.assert_called_once_with(self.crew, body)
            self.assertEqual(
                store.read_json(protocol.drain_stamp(self.crew)),
                {"id": message_id, "at": 1000.0, "landed": True, "gate": None},
            )
            clock[0] += protocol.DRAIN_INTERVAL + 1
            protocol.poll(self.crew)
            nudge.assert_called_once_with(self.crew, body)
            # Still unread much later: ring again, so a pane that died after the nudge bounces.
            clock[0] += protocol.DRAIN_LANDED_INTERVAL
            protocol.poll(self.crew)
            self.assertEqual(nudge.call_count, 2)
            nudge.assert_called_with(self.crew, None)

    def held_drain(self, clock):
        """One poll under a frozen clock with the user's draft on the composer.

        Each poll acknowledges whatever the last one reported, the way a captain's wait does.
        """
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "nudge") as nudge,
        ):
            response = protocol.poll(self.crew, ack=getattr(self, "last_delivery", None))
        self.last_delivery = response["delivery_id"]
        nudge.assert_not_called()
        return response

    def held_notices(self):
        return [n for n in self.saved()["notices"] if n["status"] == "held"]

    def test_a_draft_held_ring_escalates_once_and_never_again(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "nudge"),
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        stamp = store.read_json(protocol.drain_stamp(self.crew))
        self.assertEqual(
            (stamp["gate"], stamp["held_since"], stamp["noticed"]), (DRAFT_GATE, 1000.0, False)
        )

        clock[0] += protocol.DRAIN_INTERVAL + 1
        self.held_drain(clock)
        self.assertEqual(self.held_notices(), [])

        clock[0] = 1000.0 + protocol.HELD_NOTICE_INTERVAL
        self.assertEqual(self.held_drain(clock)["status"], "held")
        self.assertEqual(len(self.held_notices()), 1)
        summary = self.held_notices()[0]["summary"]
        for part in ("Jack", message_id, DRAFT_GATE, "5 min"):
            self.assertIn(part, summary)
        self.assertTrue(store.read_json(protocol.drain_stamp(self.crew))["noticed"])

        for _ in range(2):
            clock[0] += protocol.DRAIN_INTERVAL + 1
            self.held_drain(clock)
        self.assertEqual(len(self.held_notices()), 1)

    def test_a_landed_ring_never_notices_a_hold(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "nudge"),
        ):
            protocol.deliver(self.crew, "original", initial=True)
        clock[0] += protocol.HELD_NOTICE_INTERVAL
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=None),
            patch.object(Pane, "nudge") as nudge,
        ):
            protocol.poll(self.crew)
        nudge.assert_called_once()
        stamp = store.read_json(protocol.drain_stamp(self.crew))
        self.assertTrue(stamp["landed"])
        self.assertNotIn("held_since", stamp)
        self.assertEqual(self.held_notices(), [])
        # A later hold of the same message starts its clock over rather than escalating at once.
        clock[0] += protocol.DRAIN_LANDED_INTERVAL
        self.held_drain(clock)
        self.assertEqual(self.held_notices(), [])
        self.assertEqual(store.read_json(protocol.drain_stamp(self.crew))["held_since"], clock[0])

    def test_a_hold_past_a_float_only_stamp_still_escalates_once(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "nudge"),
        ):
            protocol.deliver(self.crew, "original", initial=True)
        store.write_json(protocol.drain_stamp(self.crew), clock[0])
        clock[0] += protocol.HELD_NOTICE_INTERVAL
        self.held_drain(clock)
        # The bare float carries no message id, so this hold is new: no notice yet.
        self.assertEqual(self.held_notices(), [])
        clock[0] += protocol.HELD_NOTICE_INTERVAL
        self.held_drain(clock)
        self.assertEqual(len(self.held_notices()), 1)

    def test_drain_tolerates_a_float_only_stamp_from_an_older_session(self):
        clock = [1000.0]
        stamp = protocol.drain_stamp(self.crew)
        with patch.object(protocol.time, "time", side_effect=lambda: clock[0]):
            with patch.object(Pane, "nudge"):
                protocol.deliver(self.crew, "original", initial=True)
            store.write_json(stamp, clock[0])
            with patch.object(Pane, "nudge") as nudge:
                protocol.poll(self.crew)
            nudge.assert_not_called()
            clock[0] += protocol.DRAIN_INTERVAL + 1
            with patch.object(Pane, "nudge") as nudge:
                protocol.poll(self.crew)
            nudge.assert_called_once()
            self.assertTrue(store.read_json(stamp)["landed"])

    def test_poll_does_not_drain_when_there_is_no_unread_mail(self):
        with patch.object(Pane, "nudge") as nudge:
            protocol.poll(self.crew)
        nudge.assert_not_called()

    def test_a_process_crash_during_the_ring_still_leaves_the_message_sent_and_mailed(self):
        with patch.object(Pane, "nudge", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                protocol.deliver(self.crew, "original", initial=True)
        message = self.saved()["messages"][0]
        self.assertEqual(message["delivery"], "sent")
        self.assertEqual(self.mail(message["id"])["state"], "queued")

    def test_one_question_correlated_answer_and_explicit_done(self):
        asked = self.command("ask", question="Which path?")
        with self.assertRaisesRegex(CaptainError, "one question"):
            self.command("ask", question="Another?")
        with self.assertRaisesRegex(CaptainError, "pending question"):
            self.command("done", report="finished")
        with self.assertRaises(CaptainError):
            protocol.deliver(self.crew, "wrong", question_id="old")
        message_id = protocol.deliver(self.crew, "src", question_id=asked["question_id"])
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [message_id])
        self.command("done", report="src/a.py changed; test passed; nothing left")
        self.assertEqual(self.saved()["state"], "done")
        first = protocol.poll(self.crew)
        self.assertEqual(first["status"], "asked")
        second = protocol.poll(self.crew, first["delivery_id"])
        self.assertEqual(second["status"], "done")
        with self.assertRaisesRegex(CaptainError, second["delivery_id"]):
            protocol.poll(self.crew)
        self.assertIsNone(protocol.poll(self.crew, second["delivery_id"])["delivery_id"])
        with self.assertRaisesRegex(CaptainError, "dismiss the crew or hand off"):
            protocol.poll(self.crew)

    def attempt(self):
        try:
            return protocol.poll(self.crew)
        except CaptainError as error:
            return error

    def test_native_finish_is_idle_and_offsets_wait_for_ack(self):
        self.event({"hook_event_name": "Stop"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.attempt(), range(2)))
        notified = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, CaptainError)]
        self.assertEqual(len(notified), 1)
        self.assertEqual(len(refused), 1)
        (left,) = notified
        self.assertEqual(left["status"], "idle")
        self.assertIn(left["delivery_id"], str(refused[0]))
        self.assertEqual(self.saved()["state"], "working")
        self.assertEqual(self.saved()["offset"], 0)
        with self.assertRaises(CaptainError):
            protocol.poll(self.crew, "wrong")
        protocol.poll(self.crew, left["delivery_id"])
        self.assertEqual(self.saved()["offset"], self.crew.events.stat().st_size)
        self.assertIsNone(protocol.poll(self.crew, left["delivery_id"])["delivery_id"])

    def test_native_working_is_silent_and_idle_notifies_once_per_message(self):
        self.event({"hook_event_name": "SessionStart"})
        self.assertEqual(protocol.poll(self.crew)["status"], "working")
        self.event({"hook_event_name": "Stop"})
        first = protocol.poll(self.crew)
        self.assertEqual(first["status"], "idle")
        protocol.poll(self.crew, first["delivery_id"])
        self.event({"hook_event_name": "Stop"})
        self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        protocol.deliver(self.crew, "followup")
        self.event({"hook_event_name": "Stop"})
        self.assertEqual(protocol.poll(self.crew)["status"], "idle")

    def test_wait_holds_through_quiet_idle_and_refuses_a_finished_assignment(self):
        self.event({"hook_event_name": "Stop"})
        idle = protocol.wait(self.crew, 0)
        self.assertEqual(idle["status"], "idle")
        protocol.poll(self.crew, idle["delivery_id"])
        self.event({"hook_event_name": "Stop"})
        self.assertEqual(protocol.wait(self.crew, 0)["status"], "timeout")
        self.command("done", report="src/a.py changed; checks pass; nothing left")
        report = protocol.wait(self.crew, 0)
        self.assertEqual(report["status"], "done")
        self.assertEqual(protocol.wait(self.crew, 0, report["delivery_id"])["status"], "idle")
        with self.assertRaisesRegex(CaptainError, "dismiss the crew or hand off"):
            protocol.wait(self.crew, 0)

    def test_claude_is_never_pane_read_but_codex_and_pi_modals_keep_notifying(self):
        with patch.object(Pane, "agent_status", side_effect=AssertionError("pane read")):
            self.crew.record["provider"] = "claude"
            with patch.object(Pane, "choice_modal", side_effect=AssertionError("pane read")):
                self.assertEqual(protocol.poll(self.crew)["status"], "working")
            for provider in ("codex", "pi"):
                with self.subTest(provider=provider):
                    self.crew.record["provider"] = provider
                    # A modal cleared and raised again is news twice; codex signals nothing else.
                    for _ in range(2):
                        with patch.object(Pane, "choice_modal", return_value=True):
                            blocked = protocol.poll(self.crew)
                        self.assertEqual(blocked["status"], "awaiting_approval")
                        protocol.poll(self.crew, blocked["delivery_id"])
                        with patch.object(Pane, "choice_modal", return_value=False):
                            self.assertIsNone(protocol.poll(self.crew)["delivery_id"])

    def test_a_wait_loop_reads_the_pane_once_a_half_minute_but_starts_at_once(self):
        self.crew.record["provider"] = "codex"
        elapsed = 0.0

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        with (
            patch.object(protocol.time, "monotonic", side_effect=lambda: elapsed),
            patch.object(protocol.time, "sleep", side_effect=advance),
            patch.object(Pane, "choice_modal", return_value=False) as modal,
        ):
            self.assertEqual(protocol.wait(self.crew, 0)["status"], "timeout")
            self.assertEqual(modal.call_count, 1)
            self.assertEqual(protocol.wait(self.crew, 70)["status"], "timeout")
            self.assertEqual(modal.call_count, 4)
        self.assertGreaterEqual(elapsed, 70)

    def test_a_second_approval_event_is_not_swallowed_by_the_first(self):
        self.event({"hook_event_name": "PermissionRequest"})
        first = protocol.poll(self.crew)
        self.assertEqual(first["status"], "awaiting_approval")
        protocol.poll(self.crew, first["delivery_id"])
        self.event({"hook_event_name": "PermissionRequest"})
        second = protocol.poll(self.crew)
        self.assertEqual(second["status"], "awaiting_approval")
        self.assertNotEqual(first["delivery_id"], second["delivery_id"])

    def test_an_event_reported_approval_is_not_repeated_by_the_live_modal_check(self):
        self.crew.record["provider"] = "codex"
        self.event({"hook_event_name": "PermissionRequest"})
        first = protocol.poll(self.crew)
        self.assertEqual(first["status"], "awaiting_approval")
        with patch.object(Pane, "choice_modal", return_value=True):
            protocol.poll(self.crew, first["delivery_id"])
            # Same still-open prompt the event already reported; must not notify twice.
            self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        with patch.object(Pane, "choice_modal", return_value=False):
            self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        with patch.object(Pane, "choice_modal", return_value=True):
            second = protocol.poll(self.crew)
        self.assertEqual(second["status"], "awaiting_approval")

    def test_identical_text_is_refused_until_a_different_message_is_sent(self):
        protocol.deliver(self.crew, "original", initial=True)
        with self.assertRaisesRegex(CaptainError, self.saved()["messages"][0]["id"]):
            protocol.deliver(self.crew, "original")
        protocol.deliver(self.crew, "second")

    def test_stale_assignment_incarnation_and_foreign_actor_are_refused(self):
        for assignment, incarnation in (("old", "first"), (self.assignment["id"], "old")):
            with self.subTest(assignment=assignment, incarnation=incarnation):
                with protocol.checkpoint(self.directory) as state:
                    with self.assertRaises(CaptainError):
                        protocol.active(state, self.crew, assignment, incarnation)
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "will", "CAPTAIN_INCARNATION": "first"},
        ):
            with self.assertRaises(CaptainError):
                self.command("done", report="forged")

    def test_done_hands_over_unread_mail_in_the_refusal_itself(self):
        """`done` pre-empts Stop delivery, so its refusal carries the body itself."""
        asked = self.command("ask", question="Which path?")
        protocol.deliver(self.crew, "src", question_id=asked["question_id"])
        with self.assertRaises(CaptainError) as refusal:
            self.command("done", report="src/a.py changed; tests pass; nothing left")
        self.assertIn("src", str(refusal.exception))
        self.assertIn("Crew name: Jack.", str(refusal.exception))
        self.assertNotIn("inbox", str(refusal.exception))
        # The refusal stamped the receipt, so the retry needs no manual read.
        self.assertEqual(protocol.unread(self.crew), [])
        self.command("done", report="src/a.py changed; tests pass; nothing left")
        self.assertEqual(self.saved()["state"], "done")

    def test_wait_returns_the_acked_done_idle_instead_of_raising_on_the_next_pass(self):
        self.command("done", report="src/a.py changed; checks pass; nothing left")
        report = protocol.wait(self.crew, 0)
        self.assertEqual(report["status"], "done")
        with patch.object(protocol.time, "sleep", side_effect=AssertionError("wait looped again")):
            final = protocol.wait(self.crew, 5, report["delivery_id"])
        self.assertEqual(final["status"], "idle")
        self.assertIsNone(final["delivery_id"])

    def test_paths_actions_overlap_and_reported_handoff(self):
        self.command("check", action="edit", path=["src/file.py"])
        for action, path in (
            ("edit", ["docs/a"]),
            ("commit", ["src/a"]),
            ("edit", ["../escape"]),
            ("edit", []),
        ):
            with self.subTest(action=action, path=path), self.assertRaises(CaptainError):
                self.command("check", action=action, path=path)
        other = Crew(
            "will", {"name": "Will", "agent": "will", "incarnation_id": "second"}, self.crew.session
        )
        with self.assertRaisesRegex(CaptainError, "overlap"):
            protocol.begin(other, self.project, "task", ["src/nested"])
        with self.assertRaisesRegex(CaptainError, "handoff"):
            protocol.begin(self.crew, self.project, "new task")
        self.command("done", report="handoff: no files changed; checks passed")
        with self.assertRaisesRegex(CaptainError, "Acknowledge"):
            protocol.begin(self.crew, self.project, "new task", handoff=self.assignment["id"])
        response = protocol.poll(self.crew)
        protocol.poll(self.crew, response["delivery_id"])
        new = protocol.begin(self.crew, self.project, "new task", handoff=self.assignment["id"])
        self.assertNotEqual(new["id"], self.assignment["id"])
        self.assertEqual(self.saved()["original_task"], "original")

    def test_symlink_escape_legacy_and_event_bounds(self):
        (self.project / "escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CaptainError):
            protocol.owned_paths(self.project, ["escape/x"])
        legacy = Crew("old", {"name": "Old", "agent": "old"}, self.crew.session)
        with self.assertRaisesRegex(CaptainError, "Legacy"):
            protocol.poll(legacy)
        self.crew.events.parent.mkdir()
        self.crew.events.write_bytes(b"x" * 65536)
        with self.assertRaisesRegex(CaptainError, "64 KiB"):
            protocol.poll(self.crew)

    def test_timeout_has_fixed_json_shape_and_no_delivery(self):
        response = protocol.wait(self.crew, 0)
        self.assertEqual(
            set(response), {"status", "delivery_id", "crew", "assignment_id", "summary"}
        )
        self.assertEqual(response["status"], "timeout")
        self.assertIsNone(response["delivery_id"])

    def test_unread_mail_is_oldest_first_and_receipt_marks_it_read(self):
        first = protocol.deliver(self.crew, "original", initial=True)
        second = protocol.deliver(self.crew, "followup")
        self.assertEqual([m["id"] for m in protocol.unread(self.crew)], [first, second])
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [first])
        self.assertEqual([m["id"] for m in protocol.unread(self.crew)], [second])
        read = self.mail(first)
        self.assertEqual(read["state"], "read")
        self.assertIsNotNone(read["read_at"])
        # A bounced message is not resurrected by a later receipt.
        protocol.bounce(self.crew, second, "pane not found")
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [second])
        self.assertEqual(self.mail(second)["state"], "bounced")

    def test_a_read_receipt_advances_the_assignment_message_and_reads_again_change_nothing(self):
        first = protocol.deliver(self.crew, "original", initial=True)
        second = protocol.deliver(self.crew, "followup")
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [first])
        self.assertEqual([m["delivery"] for m in self.saved()["messages"]], ["read", "sent"])
        before = self.saved()
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [first])
        self.assertEqual(self.saved(), before)
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [second])
        self.assertEqual([m["delivery"] for m in self.saved()["messages"]], ["read", "read"])

    def test_a_read_receipt_lands_on_the_assignment_the_mail_names(self):
        message_id = protocol.deliver(self.crew, "original", initial=True)
        path = protocol.mail_dir(self.directory, self.crew.crew_id) / f"{message_id}.json"
        record = store.read_json(path)
        record["assignment_id"] = "retired"
        store.write_json(path, record)
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [message_id])
        self.assertEqual(self.mail(message_id)["state"], "read")
        self.assertEqual(self.saved()["messages"][0]["delivery"], "sent")

    def test_dismiss_names_the_blocker_that_actually_holds(self):
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")

        def refuse(pattern):
            with patch.object(agents.runtime, "herdr") as send:
                with self.assertRaisesRegex(CaptainError, pattern):
                    agents.dismiss_crew(args, self.pane, self.project)
            send.assert_not_called()

        message_id = protocol.deliver(self.crew, "original", initial=True)
        protocol.receipt(self.crew.session.directory, self.crew.crew_id, [message_id])
        refuse("Jack has no done report")
        self.command("done", report="src/a.py changed; checks pass; nothing left")
        refuse(r"Jack has 1 unacknowledged notification\(s\)")

    def run_cli(self, *arguments):
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["--session", self.current.meta["id"], *arguments])
        return code, output.getvalue()

    def test_cli_inbox_prints_queued_mail_oldest_first_and_marks_it_read(self):
        with patch.dict(
            os.environ,
            {
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": "first",
                "CAPTAIN_ASSIGNMENT": self.assignment["id"],
            },
        ):
            code, output = self.run_cli("inbox", "Jack")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "No mail.")
        protocol.deliver(self.crew, "original", initial=True)
        protocol.deliver(self.crew, "followup")
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            code, output = self.run_cli("inbox", "Jack")
        self.assertEqual(code, 0)
        self.assertIn("original", output)
        self.assertIn("followup", output)
        self.assertLess(output.index("original"), output.index("followup"))
        self.assertEqual(protocol.unread(self.crew), [])
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "will"}):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.run_cli("inbox", "Jack")[0], 1)
        self.assertIn("only on its own assignment", error.getvalue())

    def test_launch_assignment_defaults_and_explicit_replacement(self):
        with patch.dict(
            os.environ,
            {
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": "first",
                "CAPTAIN_ASSIGNMENT": self.assignment["id"],
            },
        ):
            self.assertEqual(self.run_cli("check", "Jack", "read")[0], 0)
            code, output = self.run_cli("ask", "Jack", "Which file?")
            self.assertEqual(code, 0)
            message_id = protocol.deliver(
                self.crew, "src/a.py", question_id=json.loads(output)["question_id"]
            )
            protocol.receipt(self.crew.session.directory, self.crew.crew_id, [message_id])
            self.assertEqual(self.run_cli("done", "Jack", "--report", "Finished")[0], 0)
            event = protocol.poll(self.crew)
            while event["delivery_id"]:
                event = protocol.poll(self.crew, event["delivery_id"])
            replacement = protocol.begin(
                self.crew, self.project, "replacement", handoff=self.assignment["id"]
            )
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.run_cli("check", "Jack", "read")[0], 1)
            self.assertIn("Stale assignment ID", error.getvalue())
            self.assertEqual(
                self.run_cli("check", "Jack", "read", "--assignment", replacement["id"])[0], 0
            )
            self.assertEqual(os.environ["CAPTAIN_ASSIGNMENT"], self.assignment["id"])

    def test_launch_assignment_rejects_missing_stale_or_foreign_context(self):
        context = {
            "CAPTAIN_ROLE": "crew",
            "CAPTAIN_CREW": "jack",
            "CAPTAIN_INCARNATION": "first",
            "CAPTAIN_ASSIGNMENT": self.assignment["id"],
        }
        for key, value in (
            ("CAPTAIN_ASSIGNMENT", ""),
            ("CAPTAIN_ASSIGNMENT", "old"),
            ("CAPTAIN_INCARNATION", ""),
            ("CAPTAIN_INCARNATION", "old"),
            ("CAPTAIN_CREW", "will"),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {**context, key: value}),
            ):
                for command in (
                    ("check", "Jack", "read"),
                    ("ask", "Jack", "Question?"),
                    ("done", "Jack", "--report", "Finished"),
                ):
                    # One exemption: a merely stale assignment no longer blocks `ask`,
                    # the crew's only escalation channel (see StaleAssignmentTests). A
                    # foreign crew, wrong incarnation or missing id still reject it.
                    if (key, value, command[0]) == ("CAPTAIN_ASSIGNMENT", "old", "ask"):
                        continue
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(self.run_cli(*command)[0], 1)
        self.assertEqual(self.saved()["state"], "working")

    def test_captain_cannot_default_to_launch_assignment(self):
        with patch.dict(os.environ, {"CAPTAIN_ASSIGNMENT": self.assignment["id"]}):
            for command in (
                ("check", "Jack", "read"),
                ("ask", "Jack", "Question?"),
                ("done", "Jack", "--report", "Finished"),
            ):
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(self.run_cli(*command)[0], 1)
                self.assertIn("explicit --assignment ID", error.getvalue())

    def test_cli_question_answer_done_and_ack_round_trip(self):
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            code, output = self.run_cli(
                "ask", "Jack", "Which file?", "--assignment", self.assignment["id"]
            )
        self.assertEqual(code, 0)
        question = json.loads(output)["question_id"]
        code, output = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        event = json.loads(output)
        self.assertEqual(event["status"], "asked")
        code, repeated = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertIn(event["delivery_id"], json.loads(repeated)["summary"])
        code, _ = self.run_cli(
            "answer", "Jack", question, "src/a.py", "--assignment", self.assignment["id"]
        )
        self.assertEqual(code, 0)
        self.run_cli("wait", "Jack", "--json", "--ack", event["delivery_id"], "--timeout", "0")
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            self.run_cli("inbox", "Jack")
        code, _ = self.run_cli(
            "done",
            "Jack",
            "--assignment",
            self.assignment["id"],
            "--report",
            "src/a.py; tests pass; nothing left",
        )
        self.assertEqual(code, 0)
        event = json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1])
        self.assertEqual(event["status"], "done")
        self.assertIn("tests pass", event["summary"])
        code, output = self.run_cli(
            "wait", "Jack", "--json", "--ack", event["delivery_id"], "--timeout", "0"
        )
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["delivery_id"])

    def test_cli_json_error_is_one_object_and_does_not_adopt_legacy(self):
        del self.current.meta["crew"]["jack"]["incarnation_id"]
        store.write_json(self.current.meta_path, self.current.meta)
        before = (self.directory / "protocol.json").read_bytes()
        code, output = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["status"], "error")
        self.assertEqual((self.directory / "protocol.json").read_bytes(), before)

    def test_cli_scoped_inspection_and_session_need_no_herdr_or_state_writes(self):
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(store, "write_text", side_effect=AssertionError("state write")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "session"]), 0)
            self.assertEqual(cli.main(["memory", "path", "--scope", "repo"]), 0)
            self.assertEqual(cli.main(["memory", "show", "--scope", "repo"]), 0)
            with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}):
                self.assertEqual(cli.main(["inspect", "files"]), 0)
        self.assertIn(self.current.meta["id"], output.getvalue())
        for scope in ("session", "project", "repo"):
            self.assertEqual(
                cli.parser().parse_args(["memory", "path", "--scope", scope]).scope, scope
            )

    def test_status_read_does_not_create_state_or_call_native_done_completion(self):
        before = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        args = SimpleNamespace(session=self.current.meta["id"], all=False)
        with (
            patch.object(agents.runtime, "herdr", return_value={"agent": {"agent_status": "done"}}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.status_crew(args, self.pane, self.project)
        self.assertIn("idle", output.getvalue())
        self.assertNotIn("done", output.getvalue())
        self.assertEqual(
            {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}, before
        )

    def test_dashboard_price_refresh_requires_an_explicit_flag(self):
        for refresh in (False, True):
            args = SimpleNamespace(
                session=self.current.meta["id"], interval=None, refresh_prices=refresh
            )
            with (
                patch.object(agents.usage, "_prices") as prices,
                patch.object(agents.dashboard, "run"),
            ):
                agents.run_dashboard(args, self.pane, self.project)
            self.assertEqual(prices.call_count, int(refresh))
            if refresh:
                prices.assert_called_once_with(cached_only=False)

    def test_model_switch_checks_composer_before_terminal_input(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack", model="mid")
        with patch.object(agents.runtime, "herdr") as send:
            with self.assertRaisesRegex(CaptainError, "empty composer"):
                agents.switch_model(args, self.pane, self.project)
        send.assert_not_called()

    def test_dismiss_missing_pane_retires_record_and_releases_paths(self):
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        self.finish()
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(
            agents.runtime, "herdr", side_effect=CaptainError('Herdr: {"code": "pane_not_found"}')
        ) as close:
            agents.dismiss_crew(args, self.pane, self.project)
        close.assert_called_once_with("pane", "close", "w1:p2")
        current = sessions.read_session(self.project, self.current.meta["id"], self.pane)
        self.assertEqual(current.meta["crew"]["jack"]["status"], "dismissed")
        self.assertEqual(self.saved()["state"], "done")
        self.assertFalse(Crew.name_reserved(current, "jack"))
        replacement = Crew(
            "will", {"name": "Will", "agent": "will", "incarnation_id": "second"}, current
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_dismiss_other_close_failure_preserves_record_and_assignment(self):
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        self.finish()
        before = self.saved()
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", side_effect=CaptainError("permission_denied")),
            self.assertRaisesRegex(CaptainError, "Could not dismiss Jack: permission_denied"),
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        current = sessions.read_session(self.project, self.current.meta["id"], self.pane)
        self.assertEqual(current.meta["crew"]["jack"], self.crew.record)
        self.assertEqual(self.saved(), before)

    def test_dismiss_releases_an_assignment_whose_mail_was_never_read(self):
        """Mail the crew never read cannot demand a done report; an enqueue is not a read."""
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.deliver(self.crew, "original", initial=True)
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(agents.runtime, "herdr", return_value={}):
            agents.dismiss_crew(args, self.pane, self.project)
        saved = self.saved()
        self.assertEqual(saved["state"], "done")
        self.assertEqual(saved["report"], "Dismissed before any message was read.")
        replacement = Crew(
            "will",
            {"name": "Will", "agent": "will", "incarnation_id": "second"},
            sessions.read_session(self.project, self.current.meta["id"], self.pane),
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_dismiss_releases_an_assignment_that_never_got_a_message(self):
        """A launch that died before its first delivery: no crew is alive to run done."""
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(agents.runtime, "herdr", return_value={}):
            agents.dismiss_crew(args, self.pane, self.project)
        saved = self.saved()
        self.assertEqual(saved["state"], "done")
        self.assertEqual(saved["report"], "Dismissed before any message was read.")
        replacement = Crew(
            "will",
            {"name": "Will", "agent": "will", "incarnation_id": "second"},
            sessions.read_session(self.project, self.current.meta["id"], self.pane),
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_failed_launch_preserves_a_retryable_launcher(self):
        args = cli.parser().parse_args(
            [
                "--session",
                self.current.meta["id"],
                "crew",
                "will",
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
            ]
        )

        def herdr(*call, **kwargs):
            if call[:2] == ("pane", "split"):
                return {"pane": {"pane_id": "w1:p2", "tab_id": "w1:t1"}}
            if call[:2] == ("pane", "run"):
                self.fail("launcher was typed into an unsettled shell")
            return {}

        with (
            patch.object(agents.runtime, "herdr", side_effect=herdr),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(agents, "shell_ready_for_input", return_value=False) as ready,
            self.assertRaisesRegex(CaptainError, "launcher .* preserved.*retried") as error,
        ):
            agents.create_crew(args, self.pane, self.project)
        ready.assert_called_once_with("w1:p2")
        launcher = self.directory / "crew-will.sh"
        self.assertTrue(launcher.exists())
        self.assertNotIn("rm -f", launcher.read_text(encoding="utf-8"))
        crew_agent = sessions.agent_name(self.current.meta["id"], "will")
        self.assertIn(f"/bin/sh {launcher}", str(error.exception))
        self.assertIn(f"herdr agent rename w1:p2 {crew_agent}", str(error.exception))
        self.assertIn(f"herdr agent get {crew_agent}", str(error.exception))


class HookDeliveryTests(unittest.TestCase):
    """A crew whose own hook delivers its mail: nothing of the body crosses a terminal."""

    def setUp(self):
        ProtocolTests.setUp(self)
        self.crew.record["provider"] = "claude"

    def test_the_doorbell_carries_no_payload(self):
        with patch.object(Pane, "nudge") as nudge:
            protocol.deliver(self.crew, "original", initial=True)
            nudge.assert_called_once_with(self.crew, None)

    def test_a_retry_doorbell_is_the_same_call_as_the_first(self):
        """Nothing to branch on, so a repeat is a no-op rather than a retyped assignment."""
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge") as nudge,
        ):
            protocol.deliver(self.crew, "original", initial=True)
            clock[0] += protocol.DRAIN_LANDED_INTERVAL + 1
            protocol.drain(self.crew)
            self.assertEqual(nudge.call_args_list, [nudge.call_args_list[0]] * 2)

    def test_the_wake_line_names_no_command_to_run(self):
        from captain_barbossa.pane import WAKE_LINE, inbox_line

        self.assertEqual(inbox_line(self.crew), WAKE_LINE)
        self.assertNotIn("inbox", WAKE_LINE)


class EchoGraceTests(unittest.TestCase):
    """A composer read right after our own ring may be showing that ring, not a human."""

    def setUp(self):
        ProtocolTests.setUp(self)

    def test_a_draft_inside_the_grace_window_starts_no_hold(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=None),
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        landed = store.read_json(protocol.drain_stamp(self.crew))
        self.assertTrue(landed["landed"])
        clock[0] += protocol.ECHO_GRACE - 0.5
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
        ):
            protocol.ring(self.crew, message_id)
        # Unchanged: no hold clock was started against a crew that already has its doorbell.
        self.assertEqual(store.read_json(protocol.drain_stamp(self.crew)), landed)

    def test_a_draft_past_the_grace_window_does_hold(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=None),
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        clock[0] += protocol.ECHO_GRACE + 1
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "draft", return_value="half a sentence"),
        ):
            protocol.ring(self.crew, message_id)
        record = store.read_json(protocol.drain_stamp(self.crew))
        self.assertEqual(record["gate"], DRAFT_GATE)
        self.assertFalse(record["landed"])
        self.assertEqual(record["draft"], "half a sentence")


class BusyRingTests(unittest.TestCase):
    """A busy crew holds the ring whatever its provider; its mail waits for the boundary."""

    def setUp(self):
        ProtocolTests.setUp(self)
        self.enterContext(patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK))

    def gate(self, provider, status, composer=None):
        self.crew.record["provider"] = provider
        with (
            patch.object(Pane, "agent_status", return_value=status),
            patch.object(Pane, "lines", return_value=[]),
            patch.object(Pane, "_gate", return_value=composer),
        ):
            return self.crew.pane.nudge_block(self.crew)

    def test_a_busy_crew_holds_the_ring_whatever_its_provider(self):
        for provider in ("claude", "codex", "pi", "grok"):
            with self.subTest(provider=provider):
                self.assertEqual(self.gate(provider, "working"), "agent not idle")

    def test_an_idle_crew_is_rung(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.assertIsNone(self.gate(provider, "idle"))

    def test_an_approval_prompt_holds_the_ring(self):
        self.assertEqual(self.gate("claude", "blocked"), "approval prompt")

    def test_a_human_draft_holds_the_ring(self):
        self.assertEqual(self.gate("claude", "idle", DRAFT_GATE), DRAFT_GATE)

    def test_an_unreadable_composer_is_its_own_gate_not_a_draft(self):
        """A pane still painting at launch must not read as a human mid-sentence."""
        self.assertEqual(self.gate("claude", "idle", UNREADABLE_GATE), UNREADABLE_GATE)


class UnreadableComposerTests(unittest.TestCase):
    """Regression: a pane still painting at launch held a crew's whole task as a user draft.

    Seen live: `{"landed": false, "gate": "user draft", "draft": ""}` against a crew that
    had never drawn a composer we could parse, so its doorbell waited out DRAFT_EXPIRY.
    """

    def setUp(self):
        ProtocolTests.setUp(self)

    def test_empty_and_unreadable_are_different_facts(self):
        for text, gate in ((None, UNREADABLE_GATE), ("", None), ("half a sentence", DRAFT_GATE)):
            with self.subTest(text=text):
                with patch.object(Pane, "_composer", return_value=(text, [])):
                    self.assertEqual(self.crew.pane._gate("claude"), gate)
                    self.assertEqual(self.crew.pane.draft("claude"), text)
                    # Still fail-closed: an unreadable composer is never typed into blind.
                    self.assertEqual(self.crew.pane.draft_pending("claude"), gate is not None)

    def test_an_unreadable_composer_ages_out_on_its_own_expiry(self):
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=UNREADABLE_GATE),
            patch.object(Pane, "draft", return_value=None),
            patch.object(Pane, "nudge") as nudge,
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
            held = store.read_json(protocol.drain_stamp(self.crew))
            self.assertEqual(
                (held["gate"], held["draft_gate"], held["draft"], held["draft_since"]),
                (UNREADABLE_GATE, UNREADABLE_GATE, None, 1000.0),
            )
            self.assertFalse(held["landed"])
            nudge.assert_not_called()
            clock[0] += UNREADABLE_EXPIRY + 1
            protocol.ring(self.crew, message_id)
        nudge.assert_called_once()
        # Expired means ringable, not fusable: the inbox line alone, never the mail body.
        self.assertIsNone(nudge.call_args.args[1])
        self.assertTrue(store.read_json(protocol.drain_stamp(self.crew))["landed"])

    def test_a_human_draft_still_holds_past_the_unreadable_expiry(self):
        """The two gates age on their own clocks; a sentence someone is typing keeps its own."""
        clock = [1000.0]
        with (
            patch.object(protocol.time, "time", side_effect=lambda: clock[0]),
            patch.object(Pane, "nudge_block", return_value=DRAFT_GATE),
            patch.object(Pane, "draft", return_value="half a sentence"),
            patch.object(Pane, "nudge") as nudge,
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
            clock[0] += UNREADABLE_EXPIRY + 1
            protocol.ring(self.crew, message_id)
        nudge.assert_not_called()
        self.assertEqual(store.read_json(protocol.drain_stamp(self.crew))["gate"], DRAFT_GATE)


class StalledCrewTests(unittest.TestCase):
    """Regression: a hook-delivered crew was handed its task, never woke, and nothing said so.

    Seen live: the receipt the crew's own SessionStart hook stamps emptied drain's unread
    queue 0.56s after the mail was queued, so the held doorbell was never retried and
    `wait` reported nothing for the whole session against an idle pane.
    """

    saved = ProtocolTests.saved
    event = ProtocolTests.event

    def setUp(self):
        ProtocolTests.setUp(self)
        self.crew.record["provider"] = "claude"

    def hand_over(self, gate=UNREADABLE_GATE):
        """Deliver mail behind a held doorbell, then let the crew's own hook stamp the receipt."""
        draft = None if gate == UNREADABLE_GATE else "half a sentence"
        with (
            patch.object(Pane, "nudge_block", return_value=gate),
            patch.object(Pane, "draft", return_value=draft),
        ):
            message_id = protocol.deliver(self.crew, "original", initial=True)
        protocol.receipt(self.directory, self.crew.crew_id, [message_id])
        self.assertEqual(protocol.unread(self.crew), [])
        self.assertEqual(self.saved()["messages"][0]["delivery"], "read")
        return message_id

    def backdate(self):
        """Age the ring stamp past the drain cooldown, as a polling captain would."""
        stamp = protocol.drain_stamp(self.crew)
        record = store.read_json(stamp)
        record["at"] = time.time() - protocol.DRAIN_INTERVAL - 1
        store.write_json(stamp, record)
        return stamp

    def test_a_delivered_body_with_no_turn_is_reported_once(self):
        self.hand_over()
        stalled = protocol.poll(self.crew)
        self.assertEqual(stalled["status"], "error")
        self.assertIn("no turn has ever started", stalled["summary"])
        protocol.poll(self.crew, stalled["delivery_id"])
        self.assertIsNone(protocol.poll(self.crew)["delivery_id"])

    def test_a_started_turn_is_what_clears_the_report(self):
        self.hand_over()
        self.event({"hook_event_name": "UserPromptSubmit"})
        self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        self.assertTrue(self.saved()["turn_started"])

    def test_a_session_start_alone_is_not_a_started_turn(self):
        self.hand_over()
        self.event({"hook_event_name": "SessionStart"})
        self.assertEqual(protocol.poll(self.crew)["status"], "error")
        self.assertFalse(self.saved().get("turn_started"))

    def test_a_held_doorbell_is_still_retried_after_the_receipt(self):
        """Delivery is not liveness: a stamped receipt must not end the retries."""
        message_id = self.hand_over()
        stamp = self.backdate()
        with patch.object(Pane, "nudge") as nudge:
            protocol.drain(self.crew)
        nudge.assert_called_once()
        retried = store.read_json(stamp)
        self.assertEqual((retried["id"], retried["landed"]), (message_id, True))

    def test_a_crew_that_woke_stops_the_retries(self):
        self.hand_over()
        self.event({"hook_event_name": "UserPromptSubmit"})
        protocol.poll(self.crew)
        self.backdate()
        with patch.object(Pane, "nudge") as nudge:
            protocol.drain(self.crew)
        nudge.assert_not_called()

    def test_a_ring_delivered_body_is_no_stall(self):
        """Only a delivery hook stamps a receipt without a turn; a ring carries its own body."""
        self.crew.record["provider"] = "codex"
        self.hand_over(DRAFT_GATE)
        self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        self.backdate()
        with patch.object(Pane, "nudge") as nudge:
            protocol.drain(self.crew)
        nudge.assert_not_called()


class StaleAssignmentTests(unittest.TestCase):
    """A crew holding a retired assignment id must still be able to reach the captain.

    Seen live after `assign --handoff`: `done` and `ask` both refused, so it could
    neither report nor ask why, and the Stop hook kept re-blocking it.
    """

    command = ProtocolTests.command

    def setUp(self):
        ProtocolTests.setUp(self)
        self.first = self.assignment["id"]
        self.command("done", report="first pass done; handing off")
        pending = protocol.poll(self.crew)  # the done notice; handoff needs it acknowledged
        protocol.poll(self.crew, pending["delivery_id"])
        self.second = protocol.begin(
            self.crew, self.project, "second", ["src"], ["edit"], handoff=self.first
        )["id"]

    def crew_env(self):
        """The crew's launch-bound environment, still naming the retired assignment."""
        return patch.dict(
            os.environ,
            {
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": "first",
                "CAPTAIN_ASSIGNMENT": self.first,
            },
        )

    def run_as_crew(self, command, **kwargs):
        args = SimpleNamespace(command=command, assignment=None, incarnation="first", **kwargs)
        with self.crew_env():
            return protocol.change(self.crew, args, self.project)

    def live(self):
        return store.read_json(self.directory / "protocol.json")["assignments"][self.second]

    def test_ask_still_reaches_the_captain_on_a_stale_id(self):
        result = self.run_as_crew("ask", question="Which assignment am I on?")
        self.assertEqual(result["assignment_id"], self.second)
        self.assertEqual(self.live()["question"]["text"], "Which assignment am I on?")

    def test_done_names_the_live_assignment_so_the_crew_can_recover(self):
        with self.assertRaises(CaptainError) as refusal:
            self.run_as_crew("done", report="33 test files found under tests/")
        message = str(refusal.exception)
        self.assertIn(self.first, message)
        self.assertIn(self.second, message)
        self.assertIn(f"--assignment {self.second}", message)

    def test_done_against_the_named_live_assignment_succeeds(self):
        args = SimpleNamespace(
            command="done",
            assignment=self.second,
            incarnation="first",
            report="33 test files found under tests/",
        )
        with self.crew_env():
            self.assertEqual(protocol.change(self.crew, args, self.project)["status"], "done")


class ApprovalDedupeTests(unittest.TestCase):
    """One approval must wake the captain once, not once per event that reports it.

    Seen live three times for one approval, including after it was granted.
    """

    def setUp(self):
        ProtocolTests.setUp(self)
        self.crew.record["provider"] = "claude"

    event = ProtocolTests.event

    def surfaced(self):
        """Poll once, acknowledging anything it reports; return the status."""
        response = protocol.poll(self.crew)
        if response["delivery_id"]:
            protocol.poll(self.crew, response["delivery_id"])
        return response["status"]

    def test_the_two_events_for_one_approval_surface_once(self):
        self.event({"hook_event_name": "PermissionRequest"})
        self.assertEqual(self.surfaced(), "awaiting_approval")
        self.event({"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        self.assertEqual(self.surfaced(), "working")

    def test_a_turn_that_ran_makes_the_next_approval_news_again(self):
        self.event({"hook_event_name": "PermissionRequest"})
        self.assertEqual(self.surfaced(), "awaiting_approval")
        self.event({"hook_event_name": "Stop"})
        self.surfaced()
        self.event({"hook_event_name": "PermissionRequest"})
        self.assertEqual(self.surfaced(), "awaiting_approval")


class InterruptTests(unittest.TestCase):
    """The lifecycle channel: the only thing that reaches a crew mid-turn."""

    def setUp(self):
        ProtocolTests.setUp(self)

    def interrupt(self, name="Jack", reason=None):
        args = SimpleNamespace(name=name, session=self.current.meta["id"], reason=reason)
        return agents.interrupt_crew(args, self.pane, self.project)

    def test_it_sends_escape_and_keeps_the_assignment_open(self):
        with patch.object(agents.runtime, "herdr") as herdr:
            self.interrupt()
        herdr.assert_called_once_with("agent", "send-keys", "jack", "escape")
        saved = store.read_json(self.directory / "protocol.json")["assignments"]
        self.assertEqual(saved[self.assignment["id"]]["state"], "working")

    def test_it_rings_mail_that_the_busy_turn_held(self):
        """A captain who interrupts is usually interrupting in order to say something."""
        message_id = protocol.deliver(self.crew, "stop, wrong file", initial=True)
        with (
            patch.object(agents.runtime, "herdr"),
            patch.object(protocol, "ring") as ring,
        ):
            self.interrupt()
        rung_crew, rung_id = ring.call_args.args
        self.assertEqual((rung_crew.crew_id, rung_id), ("jack", message_id))

    def test_it_records_why(self):
        with patch.object(agents.runtime, "herdr"):
            self.interrupt(reason="editing the wrong file")
        recorded = json.dumps(store.read_json(self.current.graph))
        self.assertIn("interrupted", recorded)
        self.assertIn("editing the wrong file", recorded)

    def test_a_dismissed_crew_cannot_be_interrupted(self):
        self.crew.record["status"] = "dismissed"
        store.write_json(self.current.meta_path, self.current.meta)
        with self.assertRaisesRegex(CaptainError, "already dismissed"):
            self.interrupt()
