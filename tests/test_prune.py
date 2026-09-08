"""Regression tests for session retention (captain memory prune)."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory
from captain_barbossa.runtime import CaptainError


def agent_list(*agents_):
    return {"agents": list(agents_)}


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            )
        )
        for name in ("CAPTAIN_STATE_ROOT", "CAPTAIN_TEMP_ROOT", "CAPTAIN_SESSION"):
            os.environ.pop(name, None)
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.sessions = memory.storage(self.project) / "sessions"

    def make_session(self, session_id, age_days, captain_pane=None, crew_pane=None):
        directory = self.sessions / session_id
        directory.mkdir(parents=True)
        meta = {"id": session_id, "project": str(self.project), "workspace": "w1", "crew": {}}
        if crew_pane:
            meta["crew"]["jack"] = {
                "id": "jack",
                "agent": f"c-{session_id[:8]}-jack",
                "pane": crew_pane,
            }
        memory.write_json(directory / "session.json", meta)
        if captain_pane:
            memory.write_json(
                directory / "captain.json", {"provider": "claude", "pane": captain_pane}
            )
        (directory / "crew-jack.sh").write_text("#!/bin/sh\nexec claude\n", encoding="utf-8")
        memory.add_memory(directory / "graph.json", "jack", "report", "done")
        stamp = time.time() - age_days * 86400
        for path in (*directory.rglob("*"), directory):
            os.utime(path, (stamp, stamp))
        return directory

    def test_prunes_stale_sessions_and_keeps_recent_current_and_project_memory(self):
        stale = self.make_session("a" * 32, 30)
        recent = self.make_session("b" * 32, 1)
        current = self.make_session("c" * 32, 30)
        project_graph = memory.state_storage(self.project) / "graph.json"
        memory.add_memory(project_graph, "project", "test command", "python -m unittest")
        with patch.object(memory, "herdr", return_value=agent_list()) as api:
            removed = memory.prune_sessions(self.project, current="c" * 32)
        api.assert_called_once_with("agent", "list", timeout=10)
        self.assertEqual(removed, [stale])
        self.assertFalse(stale.exists())
        self.assertTrue(recent.is_dir())
        self.assertTrue(current.is_dir())
        self.assertTrue(project_graph.is_file())

    def test_keeps_stale_sessions_that_still_hold_a_live_agent(self):
        by_name = self.make_session("a" * 32, 30, crew_pane="w1:p9")
        by_pane = self.make_session("b" * 32, 30, captain_pane="w1:p4")
        gone = self.make_session("c" * 32, 30, captain_pane="w1:p8")
        live = agent_list(
            {"pane_id": "w1:p2", "name": f"c-{'a' * 8}-jack"},
            {"pane_id": "w1:p4", "agent": "claude"},
        )
        with patch.object(memory, "herdr", return_value=live):
            removed = memory.prune_sessions(self.project, days=7)
        self.assertEqual(removed, [gone])
        self.assertTrue(by_name.is_dir())
        self.assertTrue(by_pane.is_dir())

    def test_unreachable_herdr_only_prunes_beyond_twice_the_cutoff(self):
        young = self.make_session("a" * 32, 10)
        old = self.make_session("b" * 32, 20)
        for failure in (
            CaptainError("herdr is not installed"),
            OSError("herdr socket is gone"),
            subprocess.TimeoutExpired("herdr", 10),
        ):
            with self.subTest(failure=failure):
                with patch.object(memory, "herdr", side_effect=failure):
                    self.assertIsNone(memory.live_agents())
        with patch.object(memory, "herdr", side_effect=CaptainError("herdr is not installed")):
            removed = memory.prune_sessions(self.project, days=7)
        self.assertEqual(removed, [old])
        self.assertTrue(young.is_dir())

    def test_ignores_entries_that_are_not_session_directories(self):
        stray = self.sessions / "not-a-session"
        stray.mkdir(parents=True)
        loose = self.sessions / "graph.json"
        loose.write_text("{}", encoding="utf-8")
        stamp = time.time() - 90 * 86400
        for path in (stray, loose):
            os.utime(path, (stamp, stamp))
        with patch.object(memory, "herdr", return_value=agent_list()):
            self.assertEqual(memory.prune_sessions(self.project), [])
        self.assertTrue(stray.is_dir())
        self.assertTrue(loose.is_file())

    def test_missing_sessions_directory_prunes_nothing(self):
        with patch.object(memory, "herdr") as api:
            self.assertEqual(memory.prune_sessions(self.project), [])
        api.assert_not_called()

    def test_command_reports_what_it_removed_without_an_active_session(self):
        stale = self.make_session("a" * 32, 30)
        self.make_session("b" * 32, 1)
        out = io.StringIO()
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(memory, "herdr", return_value=agent_list()),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(cli.main(["memory", "prune"]), 0)
        self.assertIn("Pruned 1 session directory:", out.getvalue())
        self.assertIn(str(stale), out.getvalue())
        self.assertFalse(stale.exists())

    def test_command_honors_older_than_and_reports_an_empty_prune(self):
        recent = self.make_session("a" * 32, 3)
        out = io.StringIO()
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(memory, "herdr", return_value=agent_list()),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(cli.main(["memory", "prune", "--older-than", "5"]), 0)
            self.assertIn("No session memory older than 5.0 days to prune.", out.getvalue())
            self.assertTrue(recent.is_dir())
            self.assertEqual(cli.main(["memory", "prune", "--older-than", "1"]), 0)
        self.assertFalse(recent.exists())

    def test_launch_prunes_once_and_never_fails_on_retention(self):
        stale = self.make_session("a" * 32, 30)
        args = cli.parser().parse_args(["--agent", "claude"])
        with (
            patch.object(agents, "herdr"),
            patch.object(memory, "herdr", return_value=agent_list()),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(os, "execvpe") as execute,
            patch.object(sys.stdin, "isatty", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.launch(args, self.pane, self.project)
        execute.assert_called_once()
        self.assertFalse(stale.exists())
        blocked = self.make_session("b" * 32, 30)
        with (
            patch.object(agents, "herdr"),
            patch.object(agents, "prune_sessions", side_effect=OSError("state is unreadable")),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(os, "execvpe") as execute,
            patch.object(sys.stdin, "isatty", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.launch(args, self.pane, self.project)
        execute.assert_called_once()
        self.assertTrue(blocked.is_dir())


if __name__ == "__main__":
    unittest.main()
