import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli
from captain_barbossa.runtime import CaptainError
from captain_barbossa.sessions import all_sessions, session
from captain_barbossa.store import write_json

PANE = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}


class SessionResumeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        roots = {
            "CAPTAIN_STATE_ROOT": str(self.root / "state"),
            "CAPTAIN_TEMP_ROOT": str(self.root / "temp"),
        }
        self.enterContext(patch.dict(os.environ, roots))
        for name in [n for n in os.environ if n.startswith("CAPTAIN_") and n not in roots]:
            self.enterContext(patch.dict(os.environ)).pop(name)
        self.enterContext(patch.object(os, "chdir"))
        self.here = self.root / "here"
        self.other = self.root / "other"
        self.here.mkdir()
        self.other.mkdir()
        self.project = self.here

    def make(self, project):
        return session(project, PANE, create=True)

    def run_main(self, *args, tty=False):
        with (
            patch.object(cli, "project_root", side_effect=lambda: self.project),
            patch.object(cli, "current_pane", return_value=PANE),
            patch.object(cli, "launch") as launch,
            patch("sys.stdin.isatty", return_value=tty),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = cli.main(list(args))
        return code, out.getvalue(), launch

    def test_rename_names_the_session_and_lists_it(self):
        current = self.make(self.here)
        self.run_main("--session", current.meta["id"], "rename", "alpha")
        self.assertEqual(all_sessions(self.here)[0][1], "alpha")

    def test_rename_rejects_an_id_shaped_name(self):
        current = self.make(self.here)
        with self.assertRaises(CaptainError):
            cli.rename_session(current.directory, "a" * 32)

    def test_list_is_flat_and_only_contains_current_project_sessions(self):
        far = self.make(self.other)
        near = self.make(self.here)
        write_json(far.directory / "session.json", {"id": "x"})  # no project recorded
        rows = all_sessions(self.here)
        self.assertEqual([row[2] for row in rows], [near.meta["id"], far.meta["id"]])
        self.assertEqual(rows[1][0], "-")
        _, out, launch = self.run_main("resume")
        self.assertNotIn(str(self.here), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith(f"-  {near.meta['id']}  "))
        self.assertNotIn(str(self.other), out)
        self.assertNotIn("-\n", out)
        launch.assert_not_called()

    def test_resume_reports_when_current_project_has_no_sessions(self):
        self.make(self.other)
        code, out, launch = self.run_main("resume")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        launch.assert_not_called()

    def test_resume_by_name_launches_from_that_project(self):
        far = self.make(self.other)
        self.project = self.other
        self.run_main("--session", far.meta["id"], "rename", "boat")
        self.project = self.here
        code, _, launch = self.run_main("resume", "boat")
        self.assertEqual(code, 0)
        args, project = launch.call_args.args[0], launch.call_args.args[2]
        self.assertEqual((args.session, project), (far.meta["id"], self.other))
        os.chdir.assert_called_once_with(str(self.other))

    def test_resume_by_id(self):
        far = self.make(self.other)
        _, _, launch = self.run_main("resume", far.meta["id"])
        self.assertEqual(launch.call_args.args[0].session, far.meta["id"])

    def test_ambiguous_name_errors(self):
        for project in (self.here, self.other):
            current = self.make(project)
            self.project = project
            self.run_main("--session", current.meta["id"], "rename", "twin")
        self.project = self.here
        with self.assertRaisesRegex(CaptainError, "ambiguous-session"):
            cli.find_session(self.here, "twin")

    def test_tty_picks_only_from_the_current_project(self):
        self.make(self.other)
        near = self.make(self.here)
        with patch.object(cli, "choose", return_value=near.meta["id"]) as choose:
            _, _, launch = self.run_main("resume", tty=True)
        self.assertEqual(choose.call_args.args[1], [near.meta["id"]])
        self.assertEqual(len(choose.call_args.args), 5)
        self.assertEqual(launch.call_args.args[0].session, near.meta["id"])
