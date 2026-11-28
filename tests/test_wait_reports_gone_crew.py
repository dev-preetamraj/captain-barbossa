"""A crew whose native agent is gone after it started on its mail is one failure, promptly."""

import contextlib
import io
import json
import os
from unittest.mock import patch

from captain_barbossa import events, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase

ALIVE = {"agent": "claude", "agent_status": "working"}
CLOSED = CaptainError('Herdr: {"code": "pane_not_found", "message": "pane w1:p2 not found"}')


class GoneCrewTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.enterContext(patch.object(Pane, "screen", return_value=[]))
        self.crew = self.recruit("jack", "claude")

    def recruit(self, name, provider):
        """A crew in pane w1:p2 whose task is queued and rung."""
        crew = Crew(
            name,
            {
                "name": name.title(),
                "agent": name,
                "provider": provider,
                "pane": "w1:p2",
                "incarnation_id": "first",
            },
            self.current,
        )
        self.current.meta["crew"][name] = crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(crew, self.project, "original", [name], ["edit"])
        protocol.deliver(crew, "original", initial=True)
        return crew

    def hook(self, kind):
        """Claude's own hook entry: the event is appended, then the mail it carries receipted."""
        self.crew.events.parent.mkdir(exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            events.append_event([str(self.crew.events), json.dumps({"hook_event_name": kind})])

    def poll(self, pane=ALIVE, error=None, ack=None, crew=None):
        with patch.object(Pane, "pane_record", return_value=pane, side_effect=error):
            return protocol.poll(crew or self.crew, ack)

    def test_a_crew_that_died_mid_assignment_is_one_failure(self):
        """Regression: a crew read its mail, began its turn and exited; wait ran to its
        deadline. The prompt hook's own event, before its receipt, is the turn's start."""
        self.hook("UserPromptSubmit")
        self.assertTrue(protocol.active_assignment(self.crew)["messages"][0]["activated"])
        self.assertIsNone(self.poll()["delivery_id"])
        gone = self.poll({})  # the pane is back at its shell
        self.assertEqual(gone["status"], "error")
        self.assertIn("[crew-gone]", gone["summary"])
        self.assertIn("no agent in pane w1:p2", gone["summary"])
        self.poll({}, ack=gone["delivery_id"])
        self.assertIsNone(self.poll({})["delivery_id"])
        self.assertIsNone(self.poll(error=CLOSED)["delivery_id"])

    def test_a_closed_pane_is_gone_but_a_failed_read_is_not(self):
        self.hook("UserPromptSubmit")
        timeout = CaptainError("Herdr failed: socket timeout")
        self.assertIsNone(self.poll(error=timeout)["delivery_id"])
        self.assertIn("pane_not_found", self.poll(error=CLOSED)["summary"])

    def test_a_crew_that_never_started_is_not_called_gone(self):
        """At startup the mail is read at SessionStart, before Herdr detects the agent."""
        self.hook("SessionStart")
        self.assertNotIn("activated", protocol.active_assignment(self.crew)["messages"][0])
        self.assertIsNone(self.poll({})["delivery_id"])

    def test_acknowledging_crew_gone_commits_where_the_screen_is_read(self):
        """Regression: after crew-gone, a codex, pi or grok poll still read the missing
        agent's screen, which raised agent_not_found, so the ack never committed."""
        for provider in ("codex", "pi", "grok"):
            with self.subTest(provider=provider):
                crew = self.recruit(provider, provider)
                with patch.object(Pane, "lines", return_value=[]):
                    self.poll(crew=crew)  # Herdr's working after the ring landed: its turn
            with (
                self.subTest(provider=provider),
                patch.object(Pane, "lines", side_effect=CaptainError("Herdr: agent_not_found")),
            ):
                gone = self.poll({}, crew=crew)
                self.assertIn("[crew-gone]", gone["summary"])
                self.assertIsNone(self.poll({}, ack=gone["delivery_id"], crew=crew)["delivery_id"])
                assignment = protocol.active_assignment(crew)
                self.assertEqual(
                    (assignment["pending"], assignment["last_ack"]), (None, gone["delivery_id"])
                )
