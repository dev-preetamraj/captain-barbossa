"""The captain's exit takes this session's panes with it: its crew and its dashboard.

exec replaces the captain process with the native CLI, so the teardown cannot be an
atexit hook; it is a forked watcher that polls for being reparented away from the CLI.
"""

import contextlib
import io
import os
import signal
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, launch, protocol, sessions, store
from captain_barbossa.crew import Crew
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
            launch.close_session_panes(self.current.meta["id"], self.project)

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
        self.assertEqual(
            store.read_json(self.current.meta_path)["crew"]["jack"]["status"], "dismissed"
        )

    def test_each_crew_is_recorded_dismissed_as_its_pane_closes(self):
        """A resumed --session must not find a crew holding a slot no pane backs; a crew
        that never got a pane is not touched.
        """
        self.captain_record()
        self.close(lambda *args, **_: {})
        crew = store.read_json(self.current.meta_path)["crew"]
        self.assertEqual(crew["jack"]["status"], "dismissed")
        self.assertEqual(crew["gibbs"]["status"], "starting")

    def test_clean_exit_releases_ownership_and_bounces_queued_mail(self):
        for name in ("jack", "gibbs"):
            self.current.meta["crew"][name]["incarnation_id"] = name
        store.write_json(self.current.meta_path, self.current.meta)
        crew = Crew("jack", self.current.meta["crew"]["jack"], self.current)
        assignment = protocol.begin(crew, self.project, "edit", ["src"], ["edit"])
        message = protocol.deliver(crew, "edit", initial=True, queue_only=True)
        with patch.object(protocol, "unlanded", return_value=[]):
            self.close(lambda *args, **_: {})
        saved = store.read_json(self.current.directory / "protocol.json")
        self.assertEqual(saved["assignments"][assignment["id"]]["state"], "done")
        mail = store.read_json(
            protocol.mail_dir(self.current.directory, "jack") / f"{message}.json"
        )
        self.assertEqual(mail["state"], "bounced")
        other = Crew("gibbs", self.current.meta["crew"]["gibbs"], self.current)
        protocol.begin(other, self.project, "new edit", ["src"], ["edit"])

    def test_uncertain_shutdown_keeps_ownership_reserved(self):
        record = self.current.meta["crew"]["jack"]
        record.update(lifetime_id="managed", incarnation_id="jack")
        store.write_json(self.current.meta_path, self.current.meta)
        crew = Crew("jack", record, self.current)
        assignment = protocol.begin(crew, self.project, "edit", ["src"], ["edit"])
        with (
            patch.object(protocol, "unlanded", return_value=[]),
            patch.object(agents.retirement, "retire", return_value={"lifetime": {"ok": False}}),
        ):
            self.close(lambda *args, **_: {})
        saved = store.read_json(self.current.directory / "protocol.json")
        self.assertNotEqual(saved["assignments"][assignment["id"]]["state"], "done")
        self.assertEqual(
            store.read_json(self.current.meta_path)["crew"]["jack"]["status"], "cleanup_pending"
        )
        with (
            patch.object(protocol, "unlanded", return_value=[]),
            patch.object(
                agents.retirement, "retire", return_value={"lifetime": {"ok": True}}
            ) as retire,
        ):
            self.close(lambda *args, **_: {})
        retire.assert_called_once()
        self.assertEqual(
            store.read_json(self.current.meta_path)["crew"]["jack"]["status"], "dismissed"
        )
        saved = store.read_json(self.current.directory / "protocol.json")
        self.assertEqual(saved["assignments"][assignment["id"]]["state"], "done")

    def test_failed_herdr_close_preserves_mail_and_can_be_retried(self):
        record = self.current.meta["crew"]["jack"]
        record["incarnation_id"] = "jack"
        store.write_json(self.current.meta_path, self.current.meta)
        crew = Crew("jack", record, self.current)
        assignment = protocol.begin(crew, self.project, "edit", ["src"], ["edit"])
        message = protocol.deliver(crew, "edit", initial=True, queue_only=True)
        with (
            patch.object(protocol, "unlanded", return_value=[]),
            patch.object(agents.runtime, "herdr", side_effect=CaptainError("close failed")),
        ):
            launch.close_session_panes(self.current.meta["id"], self.project)
        self.assertEqual(
            store.read_json(self.current.meta_path)["crew"]["jack"]["status"], "cleanup_pending"
        )
        self.assertEqual(protocol.active_assignment(crew)["state"], "working")
        mail = protocol.mail_dir(self.current.directory, "jack") / f"{message}.json"
        self.assertEqual(store.read_json(mail)["state"], "queued")
        with patch.object(protocol, "unlanded", return_value=[]):
            self.close(lambda *args, **_: {})
        self.assertEqual(protocol.active_assignment(crew)["id"], assignment["id"])
        self.assertEqual(protocol.active_assignment(crew)["state"], "done")
        self.assertEqual(store.read_json(mail)["state"], "bounced")

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
            patch.object(launch, "close_session_panes", lambda *_: marker.write_text("done")),
            patch.object(launch, "EXIT_POLL_SECONDS", 0.05),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            child = os.fork()
            if not child:  # stands in for the captain's CLI: forks the watcher, then dies
                try:
                    launch.close_panes_on_exit(self.current.meta["id"], self.project)
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
            patch.object(launch, "close_session_panes", lambda *_: marker.write_text("done")),
            patch.object(launch, "EXIT_POLL_SECONDS", 0.05),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            child = os.fork()
            if not child:
                try:
                    launch.close_panes_on_exit(self.current.meta["id"], self.project)
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


class UnlandedWorkSurvivesExitTests(SessionCase):
    """At exit no user can confirm, so a crew holding unlanded owned work keeps its pane."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "src").mkdir()
        (self.project / "src" / "a.py").write_text("")
        self.current = sessions.session(self.project, self.pane, create=True)
        crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "incarnation_id": "first", "pane": "w1:p2"},
            self.current,
        )
        self.current.meta["crew"] = {
            "jack": crew.record,
            "gibbs": {"id": "gibbs", "name": "Gibbs", "agent": "gibbs", "pane": "w1:p3"},
        }
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(crew, self.project, "edit src", ["src"], ["edit"])

    def test_the_kept_pane_is_named_with_its_unlanded_paths(self):
        calls = []
        with (
            patch.object(
                agents.runtime, "herdr", side_effect=lambda *a, **_: calls.append(a) or {}
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            launch.close_session_panes(self.current.meta["id"], self.project)
        self.assertEqual(calls, [("pane", "close", "w1:p3")])
        self.assertIn("kept Jack's pane open", err.getvalue())
        self.assertIn("[uncommitted] src/a.py", err.getvalue())
        crew = store.read_json(self.current.meta_path)["crew"]
        self.assertNotEqual(crew["jack"].get("status"), "dismissed")
        self.assertEqual(crew["gibbs"]["status"], "dismissed")

    def test_a_failed_check_keeps_the_pane_with_the_error_as_reason(self):
        calls = []
        with (
            patch.object(protocol, "unlanded", side_effect=CaptainError("Stale crew incarnation.")),
            patch.object(
                agents.runtime, "herdr", side_effect=lambda *a, **_: calls.append(a) or {}
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            launch.close_session_panes(self.current.meta["id"], self.project)
        self.assertEqual(calls, [("pane", "close", "w1:p3")])
        self.assertIn("kept Jack's pane open", err.getvalue())
        self.assertIn("Stale crew incarnation.", err.getvalue())

    def test_inherited_git_location_vars_do_not_redirect_the_check(self):
        other = self.project.parent / "other-repo"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            reasons = protocol.unlanded(self.project, protocol.active_assignment(self.crew()))
        self.assertIn("[uncommitted] src/a.py", "; ".join(reasons))

    def test_a_globally_excluded_file_is_not_uncommitted(self):
        home = self.project.parent / "git-home"
        (home / "git").mkdir(parents=True)
        (home / "git" / "ignore").write_text(".DS_Store\n")
        (self.project / "src" / ".DS_Store").write_text("")
        env = {"XDG_CONFIG_HOME": str(home), "HOME": str(home)}
        with patch.dict(os.environ, env):
            reasons = protocol.unlanded(self.project, protocol.active_assignment(self.crew()))
        self.assertNotIn(".DS_Store", "; ".join(reasons))

    def test_a_failed_commit_query_is_unverifiable_not_unreachable(self):
        assignment = {"paths": ["src"], "report": "landed deadbeef1"}
        failed = subprocess.CompletedProcess([], 128, "", "boom")
        real = protocol._git

        def fake(project, *args):
            return failed if args[0] == "rev-parse" and "--verify" in args else real(project, *args)

        with patch.object(protocol, "_git", side_effect=fake):
            reasons = protocol.unlanded(self.project, assignment)
        text = "; ".join(reasons)
        self.assertIn("[git-failed] cannot verify commit deadbeef1", text)
        self.assertNotIn("unreachable-commit", text)

    def test_an_absent_commit_is_unreachable(self):
        assignment = {"paths": ["src"], "report": "landed deadbeef1"}
        reasons = protocol.unlanded(self.project, assignment)
        self.assertIn("[unreachable-commit] deadbeef1", "; ".join(reasons))

    def crew(self):
        return Crew("jack", self.current.meta["crew"]["jack"], self.current)


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
            patch.object(launch, "close_panes_on_exit", side_effect=OSError("no fork")),
            patch.object(agents.sys.stdin, "isatty", return_value=True),
            patch.object(os, "execvpe") as execute,
            contextlib.redirect_stderr(io.StringIO()) as problem,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            launch.launch(args, self.pane, self.project)
        execute.assert_called_once()
        self.assertIn("no exit teardown", problem.getvalue())


if __name__ == "__main__":
    unittest.main()
