"""The Claude Stop hook must refuse a turn that leaves mail unread or the assignment unfinished."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import memory, protocol
from captain_barbossa.crew import Crew
from captain_barbossa.instructions import captain_command
from tests import home_isolation  # noqa: F401


class StopHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                    "CAPTAIN_ROLE": "captain",
                },
            )
        )
        pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.current = memory.session(self.project, pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "claude", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        memory.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)

    def hook(self, event):
        """Run the hook exactly as the native CLI does and return what it printed."""
        out = io.StringIO()
        with patch("sys.argv", ["hook", str(self.crew.events)]):
            with patch("sys.stdin", io.StringIO(json.dumps(event))):
                with contextlib.redirect_stdout(out):
                    memory.append_event()
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

    def test_unread_mail_blocks_the_stop(self):
        protocol.enqueue(self.crew, "do the thing", "assign", self.assignment["id"])
        decision = self.decision({"hook_event_name": "Stop"})
        self.assertEqual(decision["decision"], "block")
        command = captain_command(self.current.directory.name)
        self.assertIn(f"{command} inbox Jack", decision["reason"])
        self.assertEqual(len(self.logged()), 1)

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

    def test_a_non_stop_event_only_appends(self):
        protocol.enqueue(self.crew, "do the thing", "assign", self.assignment["id"])
        self.assertEqual(self.hook({"hook_event_name": "SessionStart"}), "")
        self.assertEqual(json.loads(self.logged()[0])["hook_event_name"], "SessionStart")

    def test_codex_notify_never_blocks(self):
        with patch("sys.argv", ["hook", str(self.crew.events), json.dumps({"type": "x"})]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                memory.append_event()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(self.logged()[0])["type"], "x")


if __name__ == "__main__":
    unittest.main()
