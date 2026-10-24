"""Launching the captain records its provider without losing a concurrent roster write."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from tests.home_isolation import HERDR, SessionCase


class LaunchRosterWriteTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_launch_keeps_a_crew_recorded_after_its_session_read(self):
        real_session = agents.session

        def session_then_recruit(*args, **kwargs):
            current = real_session(*args, **kwargs)
            # Another shell recruits between launch's read and its provider write.
            with sessions.crew_meta(self.directory) as meta:
                meta["crew"]["jack"] = {"id": "jack", "name": "Jack", "status": "started"}
            return current

        args = cli.parser().parse_args(["--session", self.meta["id"], "--no-dashboard"])
        with (
            patch.object(agents, "session", side_effect=session_then_recruit),
            patch.object(runtime, "herdr"),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(agents, "close_panes_on_exit"),
            patch.object(os, "execvpe"),
            patch.object(sys.stdin, "isatty", return_value=True),
        ):
            args.agent = "claude"
            agents.launch(args, self.pane, self.project)
        meta = store.read_json(self.directory / "session.json")
        self.assertEqual(meta["captain_provider"], "claude")
        self.assertIn("jack", meta["crew"])


if __name__ == "__main__":
    unittest.main()
