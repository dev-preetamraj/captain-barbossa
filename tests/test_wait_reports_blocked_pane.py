import contextlib
import json
import os
from unittest.mock import patch

from captain_barbossa import protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import HERDR_ERRORS, CaptainError
from tests.home_isolation import SessionCase

PROMPT = ["Allow Write to a.py?", "1. Yes", "2. No"]


class BlockedPaneTests(SessionCase):
    def setUp(self):
        SessionCase.setUp(self)
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "claude", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "choice_modal", return_value=False))
        self.enterContext(patch.object(Pane, "lines", return_value=PROMPT))
        protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def event(self, event):
        self.crew.events.parent.mkdir(exist_ok=True)
        with self.crew.events.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def poll(self, status, ack=None):
        with patch.object(Pane, "agent_status", return_value=status):
            return protocol.poll(self.crew, ack)

    def test_claude_blocked_with_no_hook_event_is_reported(self):
        blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "awaiting_approval")
        self.assertIn("Allow Write to a.py?", blocked["summary"])

    def test_a_blank_scrollback_falls_back_to_the_visible_screen(self):
        def lines(keep_status=False, keep_blank=False, source=None):
            return ["Allow Bash: git push?", "1. Yes"] if source == "visible" else ["", " "]

        with patch.object(Pane, "lines", side_effect=lines):
            blocked = self.poll("blocked")
        self.assertIn("Allow Bash: git push?", blocked["summary"])

    def test_an_unreadable_prompt_still_names_the_pane(self):
        with patch.object(Pane, "lines", return_value=[]):
            blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "awaiting_approval")
        summary = blocked["summary"].splitlines()
        self.assertGreater(len(summary), 1)
        self.assertIn("inspect pane", summary[-1])

    def test_a_first_scrollback_read_failure_falls_back_to_the_visible_screen(self):
        def lines(keep_status=False, keep_blank=False, source=None):
            if source == "visible":
                return ["Allow Bash: git push?", "1. Yes"]
            raise HERDR_ERRORS[0]("scrollback unreadable")

        with patch.object(Pane, "lines", side_effect=lines):
            blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "awaiting_approval")
        self.assertIn("Allow Bash: git push?", blocked["summary"])

    def test_a_permanent_first_read_failure_still_surfaces(self):
        with (
            patch.object(Pane, "lines", side_effect=CaptainError("Herdr: pane_not_found")),
            self.assertRaisesRegex(CaptainError, "pane_not_found"),
        ):
            self.poll("blocked")

    def test_an_update_screen_is_not_reported_as_untrusted_hooks(self):
        self.crew.record["provider"] = "codex"
        message = protocol.deliver(self.crew, "task", initial=True, queue_only=True)
        with (
            patch.object(Pane, "nudge_block", return_value="draft"),
            patch.object(Pane, "startup_screen", return_value="Codex update prompt"),
        ):
            protocol.ring(self.crew, message)
        notices = protocol.active_assignment(self.crew)["notices"]
        self.assertIn("update prompt", notices[-1]["summary"])
        self.assertNotIn("hooks untrusted", notices[-1]["summary"].lower())

    def test_a_startup_screen_is_startup_attention_not_an_approval(self):
        self.crew.record["provider"] = "codex"
        screen = "Codex hook trust prompt"
        for live in ("blocked", "idle"):
            with self.subTest(live=live), patch.object(Pane, "startup_screen") as startup:
                startup.return_value = screen
                held = self.poll(live)
                self.assertEqual(held["status"], "error")
                with patch.object(Pane, "agent_status", return_value=live):
                    self.assertIn("startup attention", self.crew.current_state().detail)
                self.assertIn("[startup-attention]", held["summary"])
                self.assertIn(screen, held["summary"])
                self.assertIn("not approval", held["summary"])
                self.assertNotIn("retry", held["summary"])
                self.poll(live, held["delivery_id"])
                # Reported once while the screen stays up, again once it has cleared.
                self.assertIsNone(self.poll(live)["delivery_id"])
                startup.return_value = None
                self.assertIsNone(self.poll("working")["delivery_id"])

    def test_an_acknowledged_block_is_not_reported_again(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.assertIsNone(self.poll("blocked")["delivery_id"])

    def test_working_then_blocked_again_reports_again(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.assertIsNone(self.poll("working")["delivery_id"])
        self.assertEqual(self.poll("blocked")["status"], "awaiting_approval")

    def test_an_escalated_write_cleared_by_a_parallel_read_is_still_reported(self):
        self.event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "a.py"},
                "approval": {"verdict": "escalate", "code": "outside-scope"},
            }
        )
        self.event({"hook_event_name": "PreToolUse", "tool_name": "Read"})
        blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "awaiting_approval")
        self.assertIn("Write", blocked["summary"])

    def test_a_read_after_an_escalation_is_working_when_herdr_is_not_blocked(self):
        self.event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "approval": {"verdict": "escalate"},
            }
        )
        self.event({"hook_event_name": "PreToolUse", "tool_name": "Read"})
        self.assertIsNone(self.poll("working")["delivery_id"])

    def test_a_parallel_tool_event_after_the_ack_does_not_reannounce_the_block(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.event({"hook_event_name": "PreToolUse", "tool_name": "Read"})
        self.assertIsNone(self.poll("blocked")["delivery_id"])
        self.assertIsNone(self.poll("blocked")["delivery_id"])

    def test_idle_between_blocks_lets_the_next_block_be_reported(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.assertIsNone(self.poll("idle")["delivery_id"])
        self.assertEqual(self.poll("blocked")["status"], "awaiting_approval")

    def test_a_hook_notice_on_a_non_modal_poll_survives_a_parallel_read(self):
        self.event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_use_id": "t1",
                "approval": {"verdict": "escalate"},
            }
        )
        with patch.object(Pane, "agent_status", return_value="blocked"):
            first = protocol.poll(self.crew, modal=False)
            protocol.poll(self.crew, first["delivery_id"], modal=False)
        self.event({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "t2"})
        self.event({"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "t2"})
        self.assertIsNone(self.poll("blocked")["delivery_id"])

    def test_a_second_hookless_prompt_after_the_first_was_answered_is_reported(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.event({"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_use_id": "t1"})
        self.assertEqual(self.poll("blocked")["status"], "awaiting_approval")
