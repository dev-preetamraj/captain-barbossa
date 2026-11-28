import contextlib
import json
import os
import time
from unittest.mock import patch

from captain_barbossa import protocol, sessions, store
from captain_barbossa.crew import CODEX_TITLE_PROMPT, Crew
from captain_barbossa.pane import Pane, modal_start, startup_screen_name
from captain_barbossa.runtime import HERDR_ERRORS, CaptainError
from tests.home_isolation import SessionCase

PROMPT = ["Allow Write to a.py?", "1. Yes", "2. No"]
# The live Codex screen that neither modal recognizer knows.
RETIREMENT = [
    "GPT-5.6 Sol retires on December 23, 2026.",
    "Switch to GPT-6.2 Sol to continue working in Codex.",
    "› 1. Try new model",
    "2. Use existing model",
    "enter/esc confirm · ^C quit",
]


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

    def test_claude_blocked_with_no_hook_event_needs_attention(self):
        blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "needs_attention")
        self.assertIn("Allow Write to a.py?", blocked["summary"])
        self.assertIn("Herdr: blocked", blocked["summary"])

    def codex_task_landed(self):
        """A codex crew in pane w1:p2 whose ring typed its task, as in the live incident."""
        self.crew.record.update(provider="codex", pane="w1:p2")
        with patch.object(Pane, "nudge_block", return_value=None), patch.object(Pane, "nudge"):
            protocol.deliver(self.crew, "task", initial=True)
        self.assertIn("landed", protocol.active_assignment(self.crew)["messages"][0])

    def look(self, live, screen=RETIREMENT, seconds=0, **kwargs):
        """One poll with Herdr read by pane ID, the agent name still unregistered."""
        pane = {"agent": "codex", "agent_status": live}
        with (
            patch.object(Pane, "agent_status", side_effect=CaptainError("agent_not_found")),
            patch.object(Pane, "pane_record", return_value=pane),
            patch.object(Pane, "screen", return_value=screen),
            patch.object(protocol.time, "time", return_value=time.time() + seconds),
        ):
            return protocol.poll(self.crew, **kwargs)

    def test_a_screen_no_recognizer_knows_needs_attention_on_any_tick(self):
        """Seen live: a model-retirement screen held a codex crew 13 minutes in silence."""
        self.assertIsNone(modal_start(RETIREMENT))
        self.assertIsNone(startup_screen_name(RETIREMENT))
        self.codex_task_landed()
        blocked = self.look("blocked", modal=False)
        self.assertEqual(blocked["status"], "needs_attention")
        self.assertIn("GPT-5.6 Sol retires on December 23, 2026.", blocked["summary"])
        self.assertIn("w1:p2", blocked["summary"])
        self.assertIn("Herdr: blocked", blocked["summary"])
        self.assertEqual(
            protocol.active_assignment(self.crew)["attention"]["herdr"], "Herdr: blocked"
        )

    def test_landed_mail_with_no_turn_needs_attention_when_herdr_says_idle(self):
        self.codex_task_landed()
        self.assertIsNone(self.look("idle", seconds=protocol.ACTIVATION_WINDOW - 5)["delivery_id"])
        stuck = self.look("idle", seconds=protocol.ACTIVATION_WINDOW)
        self.assertEqual(stuck["status"], "needs_attention")
        self.assertIn("has started no turn", stuck["summary"])
        self.assertIn("Switch to GPT-6.2 Sol", stuck["summary"])
        self.assertIn("Herdr: idle", stuck["summary"])
        self.look("idle", seconds=protocol.ACTIVATION_WINDOW, ack=stuck["delivery_id"])
        self.assertIsNone(self.look("idle", seconds=300)["delivery_id"])

    def test_a_herdr_read_error_is_recorded_with_the_attention(self):
        self.codex_task_landed()
        with (
            patch.object(Pane, "pane_record", side_effect=HERDR_ERRORS[0]("socket timeout")),
            patch.object(Pane, "screen", return_value=RETIREMENT),
            patch.object(protocol.time, "time", return_value=time.time() + 60),
        ):
            stuck = protocol.poll(self.crew)
        self.assertEqual(stuck["status"], "needs_attention")
        self.assertIn("Herdr read failed: socket timeout", stuck["summary"])

    def test_working_after_landing_is_a_started_turn(self):
        self.codex_task_landed()
        self.look("working")
        self.assertIsNone(self.look("idle", seconds=300)["delivery_id"])

    def test_codex_title_generation_is_not_a_started_turn(self):
        """Regression: Codex's own title turn marked the task started, so no warning came."""
        self.codex_task_landed()
        self.event(
            {"type": "agent-turn-complete", "input-messages": [f"Write a {CODEX_TITLE_PROMPT}."]}
        )
        stuck = self.look("idle", seconds=protocol.ACTIVATION_WINDOW)
        self.assertEqual(stuck["status"], "needs_attention")
        self.assertNotIn("activated", protocol.active_assignment(self.crew)["messages"][0])

    def test_a_handoff_while_herdr_is_read_drops_the_old_assignments_evidence(self):
        """Regression: assignment B, handed off during A's screen read, got A's block."""
        first = protocol.active_assignment(self.crew)["id"]

        def hand_off(*args, **kwargs):
            with protocol.checkpoint(self.current.directory) as state:
                state["assignments"][first].update(state="done", report="first pass done")
            protocol.begin(self.crew, self.project, "second", ["src"], ["edit"], handoff=first)
            return PROMPT

        with patch.object(Pane, "lines", side_effect=hand_off):
            stale = self.poll("blocked")
        second = protocol.active_assignment(self.crew)
        self.assertNotEqual(second["id"], first)
        self.assertEqual((stale["assignment_id"], stale["delivery_id"]), (second["id"], None))
        self.assertNotIn("attention", second)
        self.assertEqual(self.poll("blocked")["status"], "needs_attention")

    def test_a_notice_queued_earlier_in_the_poll_is_delivered_before_attention(self):
        """Regression: hooks_live's warning and an attention notice queued in one poll; the
        warning was acknowledged unseen and the attention delivered twice."""
        self.crew.record["provider"] = "codex"
        self.event({"type": "agent-turn-complete", "input-messages": ["fix it"]})
        first = self.poll("blocked")
        self.assertIn("codex-hooks-not-live", first["summary"])
        second = self.poll("blocked", first["delivery_id"])
        self.assertEqual(second["status"], "needs_attention")
        self.assertIsNone(self.poll("blocked", second["delivery_id"])["delivery_id"])

    def test_a_blank_scrollback_falls_back_to_the_visible_screen(self):
        def lines(keep_status=False, keep_blank=False, source=None):
            return ["Allow Bash: git push?", "1. Yes"] if source == "visible" else ["", " "]

        with patch.object(Pane, "lines", side_effect=lines):
            blocked = self.poll("blocked")
        self.assertIn("Allow Bash: git push?", blocked["summary"])

    def test_an_unreadable_screen_still_names_the_pane(self):
        with patch.object(Pane, "lines", side_effect=CaptainError("Herdr: pane_not_found")):
            blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "needs_attention")
        self.assertIn("pane jack", blocked["summary"])
        self.assertIn("(unreadable)", blocked["summary"])

    def test_a_first_scrollback_read_failure_falls_back_to_the_visible_screen(self):
        def lines(keep_status=False, keep_blank=False, source=None):
            if source == "visible":
                return ["Allow Bash: git push?", "1. Yes"]
            raise HERDR_ERRORS[0]("scrollback unreadable")

        with patch.object(Pane, "lines", side_effect=lines):
            blocked = self.poll("blocked")
        self.assertEqual(blocked["status"], "needs_attention")
        self.assertIn("Allow Bash: git push?", blocked["summary"])

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
        with patch.object(Pane, "startup_screen") as startup:
            startup.return_value = screen
            held = self.poll("idle")
            self.assertEqual(held["status"], "error")
            with patch.object(Pane, "agent_status", return_value="blocked"):
                self.assertIn("startup attention", self.crew.current_state().detail)
            self.assertIn("[startup-attention]", held["summary"])
            self.assertIn(screen, held["summary"])
            self.assertIn("not approval", held["summary"])
            self.assertNotIn("retry", held["summary"])
            self.poll("idle", held["delivery_id"])
            # Reported once while the screen stays up, again once it has cleared.
            self.assertIsNone(self.poll("idle")["delivery_id"])
            startup.return_value = None
            self.assertIsNone(self.poll("working")["delivery_id"])
            startup.return_value = screen
            self.assertEqual(self.poll("idle")["status"], "error")

    def test_a_startup_screen_a_held_ring_reported_is_not_reported_again(self):
        self.crew.record["provider"] = "codex"
        trust = ["Hooks need review", "› 1. Review hooks", "2. Skip", "enter confirm · esc skip"]
        message = protocol.deliver(self.crew, "task", initial=True, queue_only=True)
        with (
            patch.object(Pane, "nudge_block", return_value="approval prompt"),
            patch.object(Pane, "startup_screen", return_value="Codex hook trust prompt"),
        ):
            protocol.ring(self.crew, message)
        untrusted = self.poll("blocked")
        self.assertIn("codex-hooks-untrusted", untrusted["summary"])
        with patch.object(Pane, "lines", return_value=trust):
            self.poll("blocked", untrusted["delivery_id"])
            self.assertIsNone(self.poll("blocked")["delivery_id"])

    def test_an_acknowledged_block_is_not_reported_again(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.assertIsNone(self.poll("blocked")["delivery_id"])

    def test_working_then_blocked_again_reports_again(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.assertIsNone(self.poll("working")["delivery_id"])
        self.assertEqual(self.poll("blocked")["status"], "needs_attention")

    def test_a_block_that_cleared_behind_an_unacked_delivery_lets_the_next_one_through(self):
        """Regression: the clearing read came after the pending early return, so block B,
        raised while A's delivery awaited its ack, was swallowed as part of A."""
        first = self.poll("blocked")
        self.assertEqual(self.poll("idle"), first)
        self.assertEqual(self.poll("blocked"), first)
        second = self.poll("blocked", first["delivery_id"])
        self.assertEqual(second["status"], "needs_attention")
        self.assertNotEqual(second["delivery_id"], first["delivery_id"])

    def test_only_a_successful_non_blocked_read_ends_the_episode(self):
        first = self.poll("blocked")
        self.poll("blocked", first["delivery_id"])
        self.event({"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_use_id": "t1"})
        self.assertIsNone(self.poll("blocked")["delivery_id"])
        with patch.object(Pane, "agent_status", side_effect=HERDR_ERRORS[0]("timeout")):
            self.assertIsNone(protocol.poll(self.crew)["delivery_id"])
        self.assertIsNone(self.poll("unknown")["delivery_id"])
        self.assertIsNone(self.poll("blocked")["delivery_id"])
        self.assertIsNone(self.poll("idle")["delivery_id"])
        self.assertEqual(self.poll("blocked")["status"], "needs_attention")

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
        self.assertEqual(self.poll("blocked")["status"], "needs_attention")

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
