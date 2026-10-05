"""The Claude Stop hook must refuse a turn that leaves mail unread or the assignment unfinished."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import cli, events, protocol, sessions, store
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase


class StopHookTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "claude", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)

    def hook(self, event):
        """Run the hook exactly as the native CLI does and return what it printed."""
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(event))):
            with contextlib.redirect_stdout(out):
                cli.main(["hook", str(self.crew.events)])
        return out.getvalue()

    def decision(self, event):
        printed = self.hook(event)
        return json.loads(printed) if printed else None

    def logged(self):
        return self.crew.events.read_text(encoding="utf-8").splitlines()

    def finish(self):
        protocol.change(
            self.crew,
            SimpleNamespace(
                command="done",
                assignment=self.assignment["id"],
                incarnation="first",
                report="src/a.py changed; checks pass; nothing left",
            ),
            self.project,
        )

    def ask(self):
        protocol.change(
            self.crew,
            SimpleNamespace(
                command="ask",
                assignment=self.assignment["id"],
                incarnation="first",
                question="Which path?",
            ),
            self.project,
        )

    def mail(self, text="do the thing"):
        return protocol.enqueue(self.crew, text, "assign", self.assignment["id"])

    def still_queued(self):
        return [m["id"] for m in protocol.queued(self.current.directory, "jack")]

    def test_stop_delivers_the_mail_itself_and_holds_the_turn_open(self):
        """Body on additionalContext; the block only keeps the turn alive."""
        self.mail()
        decision = self.decision({"hook_event_name": "Stop"})
        body = decision["hookSpecificOutput"]["additionalContext"]
        self.assertIn("do the thing", body)
        self.assertIn(self.assignment["id"], body)
        self.assertEqual(decision["decision"], "block")
        # The reason stays a short nag; the message itself is context, not an error string.
        self.assertNotIn("do the thing", decision["reason"])
        # No instruction to go and fetch it: the hook already handed it over.
        self.assertNotIn("inbox Jack", json.dumps(decision))
        self.assertEqual(self.still_queued(), [])
        self.assertEqual(len(self.logged()), 1)

    def test_user_prompt_submit_delivers_mail_as_additional_context(self):
        """An idle crew rung awake acts on its mail in the same turn the wake starts."""
        self.mail()
        decision = self.decision({"hook_event_name": "UserPromptSubmit"})
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("do the thing", output["additionalContext"])
        self.assertIn(self.assignment["id"], output["additionalContext"])
        self.assertNotIn("decision", decision)
        self.assertEqual(self.still_queued(), [])

    def test_session_start_delivers_the_launch_assignment(self):
        self.mail("build the thing")
        output = self.decision({"hook_event_name": "SessionStart"})["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("build the thing", output["additionalContext"])
        self.assertEqual(self.still_queued(), [])

    def test_identity_is_rendered_once_however_many_messages_are_waiting(self):
        self.mail("first")
        self.mail("second")
        body = self.decision({"hook_event_name": "UserPromptSubmit"})["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertEqual(body.count("Crew name: Jack."), 1)
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_a_receipt_that_fails_leaves_the_mail_queued(self):
        """Stamped only after the flush, so a lost hook re-delivers rather than loses."""
        self.mail()
        with patch("captain_barbossa.events.receipt_for", side_effect=OSError("disk")):
            printed = self.hook({"hook_event_name": "UserPromptSubmit"})
        self.assertIn("do the thing", printed)
        self.assertEqual(len(self.still_queued()), 1)

    def test_an_unfinished_assignment_blocks_the_stop(self):
        decision = self.decision({"hook_event_name": "Stop"})
        self.assertEqual(decision["decision"], "block")
        self.assertIn("captain done Jack", decision["reason"])

    def test_a_pending_question_may_stop(self):
        self.ask()
        self.assertIsNone(self.decision({"hook_event_name": "Stop"}))

    def test_a_finished_assignment_may_stop(self):
        self.finish()
        self.assertIsNone(self.decision({"hook_event_name": "Stop"}))

    def test_stop_hook_active_never_blocks_again(self):
        self.assertIsNone(self.decision({"hook_event_name": "Stop", "stop_hook_active": True}))

    def test_unreadable_state_fails_open(self):
        (self.current.directory / "protocol.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.decision({"hook_event_name": "Stop"}))
        self.assertEqual(len(self.logged()), 1)

    def test_an_unknown_crew_event_path_fails_open(self):
        (self.current.directory / "events" / "stranger.jsonl").touch()
        with patch.object(Crew, "events", Path(self.current.directory / "events/stranger.jsonl")):
            self.assertIsNone(self.decision({"hook_event_name": "Stop"}))

    def test_a_non_delivery_event_only_appends(self):
        self.mail()
        self.assertEqual(self.hook({"hook_event_name": "Notification"}), "")
        self.assertEqual(json.loads(self.logged()[0])["hook_event_name"], "Notification")
        self.assertEqual(len(self.still_queued()), 1)

    def test_codex_notify_never_blocks(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["hook", str(self.crew.events), json.dumps({"type": "x"})])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(self.logged()[0])["type"], "x")

    def test_hook_entry_skips_the_parser_and_update_check(self):
        with (
            patch.object(cli, "parser", side_effect=AssertionError("hook built the parser")),
            patch(
                "captain_barbossa.agents.check_for_update",
                side_effect=AssertionError("update check"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.main(["hook", str(self.crew.events), json.dumps({"type": "x"})])
        self.assertEqual(json.loads(self.logged()[0])["type"], "x")


if __name__ == "__main__":
    unittest.main()


class HookRegistrationTests(unittest.TestCase):
    """A delivery hook that is not registered cannot fire, and nothing else notices.

    Shipped once that way: every unit test invoked the hook directly and passed, and
    only a live crew found it. This pins the two lists together.
    """

    def registered(self):
        from captain_barbossa import instructions

        args = instructions.native_args("claude", "instructions", events=Path("/tmp/events.jsonl"))
        return set(json.loads(args[args.index("--settings") + 1])["hooks"])

    def test_every_delivery_hook_is_registered_with_the_native_cli(self):
        self.assertTrue(
            set(events.DELIVERY_HOOKS) <= self.registered(),
            f"unregistered delivery hooks: {set(events.DELIVERY_HOOKS) - self.registered()}",
        )

    def test_the_lifecycle_hooks_survive_alongside_them(self):
        self.assertTrue({"Notification", "PermissionRequest"} <= self.registered())

    def test_a_provider_without_settings_registers_nothing(self):
        from captain_barbossa import instructions

        self.assertNotIn("--settings", instructions.native_args("codex", "instructions"))
