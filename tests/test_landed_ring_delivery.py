"""A body the doorbell typed into a non-hook crew's pane counts as delivered."""

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class LandedRingDeliveryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.directory = self.current.directory
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
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])
        # The ring lands with the body; the crew works straight from it, never running inbox.
        self.message = protocol.deliver(self.crew, "original", initial=True)

    def test_dismiss_still_requires_a_report_after_the_body_landed(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(CaptainError, "no done report"),
        ):
            agents.dismiss_crew(args, self.pane, self.project)

    def test_a_later_held_ring_keeps_the_earlier_landing(self):
        # A tell to a busy crew is held; the landed task must still count as delivered.
        with patch.object(Pane, "nudge_block", return_value="agent not idle"):
            protocol.deliver(self.crew, "follow-up")
        self.assertFalse(protocol.last_ring(self.crew)["landed"])
        self.assertFalse(agents.never_read(self.crew, protocol.active_assignment(self.crew)))

    def test_done_is_not_refused_with_the_task_the_ring_already_landed(self):
        args = SimpleNamespace(
            command="done",
            assignment=self.assignment["id"],
            incarnation="first",
            report="src/a.py changed; tests pass; nothing left",
        )
        self.assertEqual(protocol.change(self.crew, args, self.project)["status"], "done")
        self.assertEqual(protocol.unread(self.crew), [])


if __name__ == "__main__":
    unittest.main()
