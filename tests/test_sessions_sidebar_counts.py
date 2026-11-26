"""`captain sessions --json` gives each app session its needs-you and unreviewed counts."""

import contextlib
import io
import json
import os
import types
import unittest
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import cli, do, protocol, sessions, supervisor
from captain_barbossa.crew import Crew


class SessionSidebarCountTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)

    def crew(self, name):
        record = {"id": name, "agent": f"c-{name}", "incarnation_id": name[0] * 32}
        record["execution"] = "supervised"
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"][name] = record
        self.current = self.current._replace(meta=meta)
        return Crew(name, record, self.current)

    def change(self, crew, command, assignment, **fields):
        args = types.SimpleNamespace(
            command=command, assignment=assignment, incarnation=None, **fields
        )
        return protocol.change(crew, args, self.project)

    def listing(self, *flags):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["sessions", *flags]), 0)
        return {
            item["id"]: item
            for project in json.loads(out.getvalue())["projects"]
            for item in project["sessions"]
        }

    def test_a_stopped_session_counts_decisions_and_unreviewed_reports(self):
        will = self.crew("will")
        asked = protocol.begin(will, self.project, "Map the repo.")["id"]
        question = self.change(will, "ask", asked, question="Which?")["question_id"]
        protocol.escalate(will, question)
        jack = self.crew("jack")
        finished = protocol.begin(jack, self.project, "Read the docs.")["id"]
        self.change(jack, "done", finished, report="Read them.")
        do.request_authorization(
            self.current.directory, do.parse(["do", "push"]), "app", ["do", "push"]
        )
        herdr = sessions.session(self.project, self.pane, create=True)
        rows = self.listing("--json")
        row = rows[self.current.meta["id"]]
        self.assertEqual((row["state"], row["needs_you"], row["unreviewed"]), ("stopped", 2, 1))
        self.assertEqual(
            (rows[herdr.meta["id"]]["needs_you"], rows[herdr.meta["id"]]["unreviewed"]),
            (None, None),
        )
        protocol.mark_reviewed(self.current.directory, finished, None, None)
        self.assertEqual(self.listing("--json")[self.current.meta["id"]]["unreviewed"], 0)
        self.assertNotIn("needs_you", self.listing()[self.current.meta["id"]])


if __name__ == "__main__":
    unittest.main()
