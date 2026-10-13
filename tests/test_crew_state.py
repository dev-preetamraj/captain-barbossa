"""Current evidence and incarnation-bound event ordering."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import events, instructions
from captain_barbossa.agents import unreserved
from captain_barbossa.crew import Crew, CrewState, declared_state, event_status
from captain_barbossa.runtime import CaptainError
from captain_barbossa.sessions import Session
from captain_barbossa.store import write_json


class CrewStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.record = {"name": "Will", "agent": "will", "incarnation_id": "first"}
        self.meta = {"crew": {"will": self.record}}
        self.session = Session(self.directory, self.meta)
        self.crew = Crew("will", self.record, self.session)
        self.crew.events.parent.mkdir()
        write_json(self.directory / "session.json", self.meta)
        self.assignment = {"incarnation_id": "first", "state": "working", "question": None}
        self.save_assignment()
        self.pane_status = self.enterContext(
            patch("captain_barbossa.crew.Pane.agent_status", return_value="idle")
        )
        self.enterContext(patch("captain_barbossa.crew.Pane.lines", return_value=[]))
        self.delivery = self.enterContext(patch("captain_barbossa.events.deliver_mail"))

    def save_assignment(self):
        write_json(
            self.directory / "protocol.json",
            {"active": {"will": "task"}, "assignments": {"task": self.assignment}},
        )

    def append(self, **event):
        events.append_event([str(self.crew.events), json.dumps(event)])

    def test_closed_enum_and_unknown_prefix_stay_visible(self):
        for state in ("working", "paused-until", "blocked", "needs-decision", "done", "failed"):
            with self.subTest(state=state):
                self.assertEqual(declared_state(f"{state}: detail", "hook").state, state)
        verdict = declared_state("finished: surprise\nsecond line", "hook")
        self.assertEqual(verdict.state, "unknown")
        self.assertIn("finished:", str(verdict))
        self.assertNotIn("\n", str(verdict))
        self.assertEqual(
            str(CrewState("working", "pane", "busy")), "state:working source:pane busy"
        )

    def test_hook_then_pane_then_mail_precedence(self):
        self.assignment.update(state="done", report="complete")
        self.save_assignment()
        self.assertEqual(self.crew.current_state(), CrewState("done", "mail", "complete"))
        self.pane_status.return_value = "working"
        self.assertEqual(self.crew.current_state().source, "pane")
        self.append(hook_event_name="PermissionRequest")
        self.assertEqual(self.crew.current_state().state, "blocked")
        self.assertEqual(self.crew.current_state().source, "hook")
        self.append(status="mystery: investigate")
        self.assertEqual(self.crew.current_state().state, "unknown")
        self.assertIn("mystery:", self.crew.current_state().detail)

    def test_turn_end_invalidates_old_working_and_never_means_done(self):
        self.append(hook_event_name="SessionStart")
        self.append(hook_event_name="Stop")
        self.assertEqual(self.crew.current_state().state, "unknown")
        self.assertEqual(event_status({"hook_event_name": "Stop"}), "done")
        self.assignment["question"] = {"text": "Which path?"}
        self.save_assignment()
        self.assertEqual(self.crew.current_state().state, "needs-decision")

    def test_missing_evidence_explains_unknown(self):
        self.pane_status.side_effect = CaptainError("unreachable")
        verdict = self.crew.current_state()
        self.assertEqual(verdict.state, "unknown")
        self.assertIn("no events file", verdict.detail)
        self.assertIn("pane unreadable", verdict.detail)

    def test_closed_pane_invalidates_old_blocked_hook(self):
        self.append(hook_event_name="PermissionRequest")
        for missing in (None, CaptainError("pane not found")):
            with self.subTest(missing=missing):
                self.pane_status.return_value = None
                self.pane_status.side_effect = missing
                verdict = self.crew.current_state()
                self.assertEqual(verdict.state, "unknown")
                self.assertIn("pane", verdict.detail)
        self.assignment.update(state="done", report="completed before pane closed")
        self.save_assignment()
        self.assertEqual(self.crew.current_state().state, "done")

    def test_stale_assignment_never_reads_new_incarnation_pane(self):
        self.assignment["incarnation_id"] = "second"
        self.save_assignment()
        verdict = self.crew.current_state()
        self.assertEqual(verdict, CrewState("unknown", "none", "stale incarnation"))
        self.pane_status.assert_not_called()

    def test_sequence_is_monotonic_and_rejects_duplicates(self):
        self.append(hook_event_name="SessionStart")
        self.append(status="blocked", seq=5)
        self.append(status="done", seq=4)
        self.append(status="done", seq=5)
        self.append(status="done", seq=True)
        self.append(status="working")
        batch, _ = events.read_events(self.crew.events, 0)
        self.assertEqual([event["seq"] for event in batch], [1, 5, 6])
        self.assertTrue(all(event["incarnation_id"] == "first" for event in batch))

    def test_stale_roster_blocks_old_snapshot(self):
        self.meta["crew"]["will"] = {**self.record, "incarnation_id": "second"}
        write_json(self.directory / "session.json", self.meta)
        verdict = self.crew.current_state()
        self.assertEqual(verdict.state, "unknown")
        self.assertIn("Stale crew incarnation", verdict.detail)
        self.pane_status.assert_not_called()

    def test_stale_path_and_payload_neither_append_nor_deliver(self):
        self.append(hook_event_name="Stop", incarnation_id="old")
        self.assertFalse(self.crew.events.exists())
        old_path = self.crew.events
        self.record["incarnation_id"] = "second"
        write_json(self.directory / "session.json", self.meta)
        events.append_event([str(old_path), json.dumps({"hook_event_name": "Stop"})])
        self.assertFalse(old_path.exists())
        self.delivery.assert_not_called()
        self.append(status="working")
        batch, _ = events.read_events(self.crew.events, 0)
        self.assertEqual(batch[0]["seq"], 1)

    def test_reader_filters_stale_and_backward_events_across_offsets(self):
        self.append(status="working", seq=3)
        offset = self.crew.events.stat().st_size
        with self.crew.events.open("a") as stream:
            for event in (
                {"status": "done", "seq": 9, "incarnation_id": "old"},
                {"status": "done", "seq": 2, "incarnation_id": "first"},
                {"status": "blocked", "seq": 4, "incarnation_id": "first"},
            ):
                stream.write(json.dumps(event) + "\n")
        batch, end = events.read_events(self.crew.events, offset, incarnation="first")
        self.assertEqual([event["seq"] for event in batch], [4])
        self.assertEqual(end, self.crew.events.stat().st_size)
        self.assertEqual(self.crew.current_state().state, "blocked")

    def test_captain_and_legacy_logs_remain_supported(self):
        captain = self.crew.events.parent / "captain.jsonl"
        events.append_event([str(captain), '{"type": "agent-turn-complete"}'])
        event = events.read_events(captain, 0)[0][0]
        self.assertEqual(event["type"], "agent-turn-complete")
        self.assertIsNotNone(datetime.fromisoformat(event["timestamp"]).tzinfo)

    def test_native_timestamp_is_preserved(self):
        self.append(timestamp="2026-10-13T00:00:00Z", hook_event_name="SessionStart")
        self.assertEqual(
            events.read_events(self.crew.events, 0)[0][0]["timestamp"],
            "2026-10-13T00:00:00Z",
        )

    def test_tool_activity_clears_old_permission_prompt(self):
        args = instructions.native_args("claude", "crew", events=self.crew.events)
        hooks = json.loads(args[args.index("--settings") + 1])["hooks"]
        for kind in ("PostToolUse", "PostToolUseFailure"):
            with self.subTest(kind=kind):
                self.assertIn(kind, hooks)
                self.append(hook_event_name="Notification", notification_type="permission_prompt")
                self.append(hook_event_name=kind)
                self.assertEqual(self.crew.current_state().state, "working")

    def test_unknown_tool_defers_but_records_working_evidence(self):
        with patch(
            "captain_barbossa.events.permission_decision",
            return_value=events.Decision("unknown-tool"),
        ):
            self.append(hook_event_name="Notification", notification_type="permission_prompt")
            self.append(hook_event_name="PreToolUse", tool_name="CustomTool")
        self.assertEqual(self.crew.current_state().state, "working")

    def test_old_hook_cannot_read_current_mail(self):
        with self.assertRaisesRegex(CaptainError, "Stale crew incarnation"):
            events.crew_for_events(self.crew.events.parent / "will-old.jsonl")

    def test_only_a_background_crew_without_its_own_assignment_is_unreserved(self):
        self.assertFalse(unreserved(self.crew))
        self.record["execution"] = "background"
        self.assertFalse(unreserved(self.crew))
        self.assignment["incarnation_id"] = "older"
        self.save_assignment()
        self.assertTrue(unreserved(self.crew))
        (self.directory / "protocol.json").unlink()
        self.assertTrue(unreserved(self.crew))
