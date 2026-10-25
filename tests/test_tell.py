import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, runtime, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase


class TellCrewTests(SessionCase):
    """Follow-ups are mail on the crew's live assignment (agents.tell_crew)."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.assignment = protocol.begin(self.crew, self.project, "build", ["src"], ["edit"])

    def tell(self, name, message, *extra):
        args = cli.parser().parse_args(
            ["--session", self.current.meta["id"], "tell", name, message, *extra]
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            agents.tell_crew(args, self.pane, self.project)
        return output.getvalue()

    def edit_record(self, **changes):
        meta = store.read_json(self.current.meta_path)
        record = {**meta["crew"]["jack"], **changes}
        meta["crew"]["jack"] = {key: value for key, value in record.items() if value is not None}
        store.write_json(self.current.meta_path, meta)

    def test_follow_up_is_queued_as_mail_on_the_named_assignment(self):
        output = json.loads(
            self.tell("Jack", "now do the docs", "--assignment", self.assignment["id"])
        )
        self.assertEqual(output["crew"], "Jack")
        mail = protocol.unread(self.crew)
        self.assertEqual(
            [(m["id"], m["text"]) for m in mail], [(output["message_id"], "now do the docs")]
        )

    def test_follow_up_requires_the_assignment_id(self):
        with self.assertRaisesRegex(agents.CaptainError, "--assignment"):
            self.tell("Jack", "keep going")

    def test_legacy_crew_is_told_to_recruit_a_fresh_crew(self):
        self.edit_record(incarnation_id=None)
        with patch.object(runtime, "herdr", side_effect=AssertionError("herdr")):
            with self.assertRaisesRegex(agents.CaptainError, "Legacy crew Jack.*fresh crew"):
                self.tell("Jack", "keep going", "--assignment", self.assignment["id"])
        self.assertFalse((self.current.directory / "mail" / "jack").exists())

    def test_dismissed_crew_is_refused(self):
        self.edit_record(status="dismissed")
        with self.assertRaisesRegex(agents.CaptainError, "dismissed"):
            self.tell("Jack", "keep going", "--assignment", self.assignment["id"])

    def test_blank_message_is_refused(self):
        with self.assertRaisesRegex(agents.CaptainError, "1–8000 characters"):
            self.tell("Jack", "   ", "--assignment", self.assignment["id"])


if __name__ == "__main__":
    unittest.main()
