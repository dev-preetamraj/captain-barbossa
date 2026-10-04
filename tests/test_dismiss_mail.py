import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from tests.home_isolation import SessionCase


class DismissMailTests(SessionCase):
    """Regression: dismiss_crew() must bounce mail a crew never got to read, not drop it."""

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
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None, create=True))
        self.enterContext(patch.object(Pane, "nudge", create=True))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def mail(self, message_id):
        path = protocol.mail_dir(self.directory, self.crew.crew_id) / f"{message_id}.json"
        return store.read_json(path)

    def dismiss(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        return output.getvalue()

    def test_dismiss_bounces_mail_the_crew_never_read(self):
        """A sent message the crew never read leaves its mail file queued; dismiss must bounce it."""
        message_id = protocol.deliver(self.crew, "original", initial=True)
        with protocol.checkpoint(self.directory) as state:
            assignment = protocol.active(state, self.crew, self.assignment["id"])
            assignment["state"] = "done"
        self.assertEqual(self.mail(message_id)["state"], "queued")
        output = self.dismiss()
        self.assertIn(message_id, output)
        self.assertIn("Dismissed Jack.", output)
        mail = self.mail(message_id)
        self.assertEqual(mail["state"], "bounced")
        self.assertIn("dismiss", mail["reason"].lower())

    def test_dismiss_with_no_mail_prints_no_bounce_line(self):
        with protocol.checkpoint(self.directory) as state:
            assignment = protocol.active(state, self.crew, self.assignment["id"])
            assignment["state"] = "done"
        output = self.dismiss()
        self.assertNotIn("Bounced", output)
        self.assertIn("Dismissed Jack.", output)


if __name__ == "__main__":
    unittest.main()
