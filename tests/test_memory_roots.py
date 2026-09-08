"""Regression tests for the state/temp root split (durable project-scope memory)."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, memory
from captain_barbossa.runtime import CaptainError


class MemoryRootTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}

    def clean_env(self, **extra):
        return patch.dict(os.environ, {"HOME": str(self.home), **extra}, clear=True)

    def test_state_and_temp_roots_differ_by_default(self):
        with self.clean_env():
            state = memory.state_root()
            temp = memory.temp_root()
        self.assertEqual(state, self.home / ".local" / "state" / "captain-barbossa")
        self.assertEqual(temp, Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}")
        self.assertNotEqual(state, temp)

    def test_xdg_state_home_overrides_default_state_root_only(self):
        xdg = self.root / "xdg-state"
        with self.clean_env(XDG_STATE_HOME=str(xdg)):
            self.assertEqual(memory.state_root(), xdg / "captain-barbossa")
            self.assertEqual(
                memory.temp_root(), Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}"
            )

    def test_captain_memory_root_overrides_both_roots(self):
        override = self.root / "override"
        with self.clean_env(CAPTAIN_MEMORY_ROOT=str(override)):
            self.assertEqual(memory.state_root(), override)
            self.assertEqual(memory.temp_root(), override)

    def test_state_storage_rejects_root_inside_project(self):
        with self.clean_env(CAPTAIN_MEMORY_ROOT=str(self.project / ".memory")):
            with self.assertRaisesRegex(CaptainError, "outside the project"):
                memory.state_storage(self.project)

    def test_project_scope_memory_lands_in_state_root_by_default(self):
        with self.clean_env():
            directory, meta = memory.session(self.project, self.pane, create=True)
            with contextlib.redirect_stdout(io.StringIO()):
                args = cli.parser().parse_args(
                    [
                        "--session",
                        meta["id"],
                        "memory",
                        "add",
                        "project",
                        "uses",
                        "Python",
                        "--scope",
                        "project",
                    ]
                )
                memory.memory(args, self.pane, self.project)
                args = cli.parser().parse_args(
                    ["--session", meta["id"], "memory", "add", "task", "has", "secret"]
                )
                memory.memory(args, self.pane, self.project)

            state_graph = memory.state_storage(self.project) / "graph.json"
            self.assertTrue(state_graph.exists())
            self.assertNotIn("secret", state_graph.read_text())
            self.assertNotIn("Python", (directory / "graph.json").read_text())

            with contextlib.redirect_stdout(io.StringIO()) as output:
                args = cli.parser().parse_args(["--session", meta["id"], "memory", "show"])
                memory.memory(args, self.pane, self.project)
        lines = output.getvalue().splitlines()
        self.assertIn('[project] ["project", "uses", "Python"]', lines)
        self.assertIn('[session] ["task", "has", "secret"]', lines)

    def test_memory_snapshot_survives_temp_root_cleared_between_runs(self):
        with self.clean_env():
            directory, meta = memory.session(self.project, self.pane, create=True)
            memory.add_memory(
                memory.state_storage(self.project) / "graph.json", "project", "uses", "Python"
            )
            with memory.memory_snapshot(directory) as snapshot:
                graph = memory.read_json(snapshot / "graph.json")
            self.assertEqual(len(graph["links"]), 1)

            # simulate the OS purging the temp/session tree: state data must still be found
            other_directory, _ = memory.session(self.project, self.pane, meta["id"])
            with memory.memory_snapshot(other_directory) as snapshot:
                graph = memory.read_json(snapshot / "graph.json")
        self.assertEqual(len(graph["links"]), 1)


if __name__ == "__main__":
    unittest.main()
