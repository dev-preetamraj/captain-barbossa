"""The captain's exit takes this session's panes with it: its crew and its dashboard.

exec replaces the captain process with the native CLI, so the teardown cannot be an
atexit hook; it is a forked watcher that polls for being reparented away from the CLI.
"""

import contextlib
import io
import os
import signal
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, sessions, store
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class CloseSessionPanesTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.current.meta["crew"] = {
            "jack": {"id": "jack", "name": "Jack", "agent": "jack", "pane": "w1:p2"},
            "will": {
                "id": "will",
                "name": "Will",
                "agent": "will",
                "pane": "w1:p3",
                "status": "dismissed",
            },
            "gibbs": {"id": "gibbs", "name": "Gibbs", "agent": "gibbs", "status": "starting"},
        }
        store.write_json(self.current.meta_path, self.current.meta)
        self.board = None

    def captain_record(self):
        store.write_json(
            self.current.directory / "captain.json",
            {"provider": "claude", "pane": self.pane["pane_id"], "dashboard": self.board},
        )

    def close(self, herdr):
        with patch.object(agents.runtime, "herdr", side_effect=herdr):
            agents.close_session_panes(self.current.meta["id"], self.project)

    def test_live_crew_panes_and_the_dashboard_close_and_nothing_else(self):
        self.board = "w1:p9"
        self.captain_record()
        calls = []
        self.close(lambda *args, **_: calls.append(args) or {})
        self.assertEqual(calls, [("pane", "close", "w1:p2"), ("pane", "close", "w1:p9")])

    def test_a_session_with_no_dashboard_closes_only_its_crew(self):
        self.captain_record()
        calls = []
        self.close(lambda *args, **_: calls.append(args) or {})
        self.assertEqual(calls, [("pane", "close", "w1:p2")])

    def test_a_pane_already_gone_does_not_hold_up_the_rest(self):
        self.board = "w1:p9"
        self.captain_record()
        calls = []

        def herdr(*args, **_):
            calls.append(args)
            if args[2] == "w1:p2":
                raise CaptainError("pane_not_found")
            return {}

        self.close(herdr)
        self.assertEqual(calls[-1], ("pane", "close", "w1:p9"))

    def test_each_crew_is_recorded_dismissed_as_its_pane_closes(self):
        """A resumed --session must not find a crew holding a slot no pane backs; a crew
        that never got a pane is not touched.
        """
        self.captain_record()
        self.close(lambda *args, **_: {})
        crew = store.read_json(self.current.meta_path)["crew"]
        self.assertEqual(crew["jack"]["status"], "dismissed")
        self.assertEqual(crew["gibbs"]["status"], "starting")

    def await_marker(self, marker):
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(marker.read_text(), "done")

    def test_the_watcher_closes_the_panes_once_the_cli_exits(self):
        """The mechanic itself: a process standing in for the exec'd CLI exits, and the
        watcher it left behind runs the teardown.
        """
        marker = self.root / "closed"
        with (
            patch.object(agents, "close_session_panes", lambda *_: marker.write_text("done")),
            patch.object(agents, "EXIT_POLL_SECONDS", 0.05),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            child = os.fork()
            if not child:  # stands in for the captain's CLI: forks the watcher, then dies
                try:
                    agents.close_panes_on_exit(self.current.meta["id"], self.project)
                finally:
                    os._exit(0)
            os.waitpid(child, 0)
            self.await_marker(marker)

    def test_a_descendant_outliving_the_cli_does_not_hold_up_the_teardown(self):
        """The regression: an inherited pipe was held open by anything the CLI forked, so
        one backgrounded job meant the panes were never closed at all.
        """
        marker, lingering = self.root / "closed", self.root / "lingering"
        with (
            patch.object(agents, "close_session_panes", lambda *_: marker.write_text("done")),
            patch.object(agents, "EXIT_POLL_SECONDS", 0.05),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            child = os.fork()
            if not child:
                try:
                    agents.close_panes_on_exit(self.current.meta["id"], self.project)
                    job = os.fork()  # a backgrounded job of the CLI, alive past its death
                    if not job:
                        time.sleep(30)
                        os._exit(0)
                    lingering.write_text(str(job))
                finally:
                    os._exit(0)
            os.waitpid(child, 0)
            try:
                self.await_marker(marker)
            finally:
                with contextlib.suppress(OSError, ValueError):
                    os.kill(int(lingering.read_text()), signal.SIGKILL)


class LaunchTeardownTests(SessionCase):
    """A teardown that cannot be set up is reported, never fatal."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)

    def test_a_watcher_that_cannot_fork_does_not_cost_the_launch(self):
        args = SimpleNamespace(
            agent="claude", session=self.current.meta["id"], prompt=None, no_dashboard=True
        )
        with (
            patch.object(agents.runtime, "herdr", return_value={}),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(agents, "close_panes_on_exit", side_effect=OSError("no fork")),
            patch.object(agents.sys.stdin, "isatty", return_value=True),
            patch.object(os, "execvpe") as execute,
            contextlib.redirect_stderr(io.StringIO()) as problem,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.launch(args, self.pane, self.project)
        execute.assert_called_once()
        self.assertIn("no exit teardown", problem.getvalue())


if __name__ == "__main__":
    unittest.main()
