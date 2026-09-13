import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli
from captain_barbossa.runtime import CaptainError


class SessionCommandTests(unittest.TestCase):
    """Regression tests for `captain session` (cli.print_session)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.root / "state")}))
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}

    def run_main(self, *args):
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_resolved_session_id(self):
        code, out, _ = self.run_main("--session", "abc123", "session")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "abc123")

    def test_falls_back_to_captain_session_env_var(self):
        with patch.dict(os.environ, {"CAPTAIN_SESSION": "envsession"}):
            code, out, _ = self.run_main("session")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "envsession")

    def test_errors_clearly_when_no_session_is_set(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAPTAIN_SESSION", None)
            code, out, err = self.run_main("session")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Start captain first, or pass --session <id>.", err)

    def test_print_session_raises_without_a_session(self):
        args = cli.parser().parse_args(["session"])
        with patch.dict(os.environ, {}, clear=True):
            args.session = None
            with self.assertRaisesRegex(CaptainError, "Start captain first"):
                cli.print_session(args)


if __name__ == "__main__":
    unittest.main()
