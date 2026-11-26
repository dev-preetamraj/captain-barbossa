"""`captain end` ends a stopped app session with no supervisor, and never a running one."""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import cli, sessions, store, supervision, supervisor


class EndStoppedSessionTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_stopped_session_ends_with_either_spelling_and_stays_ended(self):
        session_id = self.current.meta["id"]
        code, out, _ = self.run_cli("--session", session_id, "end")
        self.assertEqual(code, 0)
        ended = json.loads(out)
        self.assertEqual(ended["session_id"], session_id)
        self.assertEqual(store.read_json(self.current.meta_path)["ended_at"], ended["ended_at"])
        code, out, _ = self.run_cli("end", "--session", session_id)
        self.assertEqual((code, json.loads(out)), (0, ended))
        (row,) = [
            item
            for project in sessions.catalog(self.project)["projects"]
            for item in project["sessions"]
            if item["id"] == session_id
        ]
        self.assertEqual(row["state"], "ended")

    def test_a_running_supervisor_or_a_herdr_session_is_refused(self):
        with store.lock(self.current.directory / supervision.LOCK):
            code, out, err = self.run_cli("--session", self.current.meta["id"], "end")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("[supervisor-running]", err)
        self.assertNotIn("ended_at", store.read_json(self.current.meta_path))
        herdr = sessions.session(self.project, self.pane, create=True)
        code, _, err = self.run_cli("--session", herdr.meta["id"], "end")
        self.assertEqual(code, 1)
        self.assertIn("[supervisor-herdr-session]", err)


if __name__ == "__main__":
    unittest.main()
