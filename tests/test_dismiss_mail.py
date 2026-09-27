import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, memory, protocol
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from tests import home_isolation  # noqa: F401


class DismissMailTests(unittest.TestCase):
    """Regression: dismiss_crew() must bounce mail a crew never got to read, not drop it."""

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
                },
            )
        )
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.current = memory.session(self.project, self.pane, create=True)
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
        memory.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None, create=True))
        self.enterContext(patch.object(Pane, "nudge", create=True))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def mail(self, message_id):
        path = protocol.mail_dir(self.directory, self.crew.crew_id) / f"{message_id}.json"
        return memory.read_json(path)

    def dismiss(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        return output.getvalue()

    def test_dismiss_bounces_mail_left_over_from_a_cancelled_message(self):
        """A message resolved to cancelled leaves its mail file queued; dismiss must bounce it."""
        message_id = protocol.deliver(self.crew, "original", initial=True)
        with protocol.checkpoint(self.directory) as state:
            assignment = protocol.active(state, self.crew, self.assignment["id"])
            assignment["messages"][0]["delivery"] = "cancelled"
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
