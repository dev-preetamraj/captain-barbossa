import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory


class WaitCrewMemoryTests(unittest.TestCase):
    """Regression tests for wait_crew's graph memory writes (agents.wait_crew, ~L381)."""

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
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": agent_name,
                "provider": "claude",
                "pane": "w1:p2",
                "tab": "w1:t1",
                "status": "started",
            }
        }
        memory.write_json(self.directory / "session.json", self.meta)
        self.agent_name = agent_name

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def wait_api(self, statuses, tail="", report=None):
        remaining = list(statuses)

        def api(*args, **kwargs):
            if args[:2] == ("agent", "get"):
                status = remaining.pop(0) if remaining else "working"
                if report and status in ("idle", "blocked"):
                    memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                return {"agent": {"agent_status": status}}
            if args[:2] == ("agent", "read"):
                return tail
            raise AssertionError(f"unexpected herdr call: {args}")

        return api

    def run_wait(self, statuses, tail="", report=None):
        with (
            patch.object(agents, "herdr", side_effect=self.wait_api(statuses, tail, report)),
            patch.object(agents.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "60"), self.pane, self.project)
        return output.getvalue()

    def edges(self, relation):
        path = self.directory / "graph.json"
        if not path.exists():
            return []
        graph = memory.read_json(path)
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        return [
            labels[link["target"]]
            for link in graph["links"]
            if link["relation"] == relation and labels[link["source"]] == "Jack"
        ]

    def test_completion_with_a_report_omits_the_pane_tail(self):
        printed = self.run_wait(["idle", "idle", "idle"], report="tests pass")
        self.assertEqual(self.edges("completed"), ["idle; reported: tests pass"])
        self.assertEqual(self.edges("tail"), [])
        self.assertNotIn("pane tail", printed)

    def test_completion_writes_exactly_once(self):
        self.run_wait(["idle", "idle", "idle"], report="tests pass")
        self.assertEqual(len(self.edges("completed")), 1)

    def test_no_report_records_a_short_tail_edge_but_prints_the_full_tail(self):
        long_tail = "x" * 500
        printed = self.run_wait(["idle", "idle", "idle"], tail=long_tail)
        self.assertEqual(self.edges("completed"), ["idle; no report recorded"])
        tail_edges = self.edges("tail")
        self.assertEqual(len(tail_edges), 1)
        self.assertLessEqual(len(tail_edges[0]), 300)
        self.assertIn(f"pane tail: {long_tail}", printed)


if __name__ == "__main__":
    unittest.main()
