"""Regression tests for bounding `captain memory show` output."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, memory


class MemoryShowBoundTests(unittest.TestCase):
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
                },
            )
        )
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def show(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show", *extra), self.pane, self.project)
        return output.getvalue().splitlines()

    def _seed(self, project_count, session_count):
        project_graph = memory.state_storage(self.project) / "graph.json"
        for i in range(project_count):
            memory.add_memory(project_graph, "project", "has", f"p{i}")
        for i in range(session_count):
            memory.add_memory(self.directory / "graph.json", "session", "has", f"s{i}")

    def test_default_show_bounds_to_25_most_recent_links(self):
        self._seed(project_count=15, session_count=30)

        lines = self.show()
        self.assertEqual(lines[0], "Memory (subject, relation, object):")
        rows = lines[1:]
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(row.startswith("[session] ") for row in rows[:20]))
        self.assertTrue(all(row.startswith("[project] ") for row in rows[20:]))

        session_targets = [json.loads(row[len("[session] ") :])[2] for row in rows[:20]]
        self.assertEqual(session_targets, [f"s{i}" for i in reversed(range(30))][:20])
        project_targets = [json.loads(row[len("[project] ") :])[2] for row in rows[20:]]
        self.assertEqual(project_targets, [f"p{i}" for i in reversed(range(15))][:5])

    def test_busy_session_does_not_crowd_out_project_slice(self):
        # A session graph well past the 25-row bound must not push project
        # facts off the end entirely: project keeps its reserved slice.
        self._seed(project_count=3, session_count=40)

        lines = self.show()
        rows = lines[1:]
        self.assertEqual(len(rows), 25)
        project_rows = [row for row in rows if row.startswith("[project] ")]
        self.assertEqual(len(project_rows), 3)

    def test_sparse_project_lets_session_fill_unused_slots(self):
        # Fewer than PROJECT_RESERVE project rows should hand the leftover
        # slots to session rows instead of leaving them unused.
        self._seed(project_count=1, session_count=30)

        lines = self.show()
        rows = lines[1:]
        self.assertEqual(len(rows), 25)
        self.assertEqual(sum(row.startswith("[session] ") for row in rows), 24)
        self.assertEqual(sum(row.startswith("[project] ") for row in rows), 1)

    def test_all_flag_shows_every_link(self):
        self._seed(project_count=15, session_count=20)
        self.assertEqual(len(self.show("--all")) - 1, 35)

    def test_below_limit_is_unaffected_by_bound(self):
        self._seed(project_count=1, session_count=2)
        self.assertEqual(len(self.show()) - 1, 3)

    def test_json_output_is_unbounded_and_unprefixed(self):
        self._seed(project_count=0, session_count=30)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show", "--json"), self.pane, self.project)
        graph = json.loads(output.getvalue())
        self.assertEqual(len(graph["links"]), 30)

    def test_scope_prefix_on_single_session_fact(self):
        memory.add_memory(self.directory / "graph.json", "task", "has", "fact")
        lines = self.show()
        self.assertEqual(lines[1], '[session] ["task", "has", "fact"]')

    def test_scope_prefix_on_single_project_fact(self):
        memory.add_memory(memory.state_storage(self.project) / "graph.json", "task", "has", "fact")
        lines = self.show()
        self.assertEqual(lines[1], '[project] ["task", "has", "fact"]')


if __name__ == "__main__":
    unittest.main()
