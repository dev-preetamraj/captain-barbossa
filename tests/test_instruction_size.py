import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory


class InstructionSizeTests(unittest.TestCase):
    """Regression tests for the generated instruction sizes (agents.agent_instructions)."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_crew_instructions_are_much_shorter_than_the_captains(self):
        crew_text = agents.agent_instructions(self.directory, "crew member Jack")
        captain_text = agents.agent_instructions(self.directory, "Captain Barbossa")
        crew_words = len(crew_text.split())
        captain_words = len(captain_text.split())
        self.assertLess(crew_words, captain_words)
        # Guards against the crew memory block creeping back toward the captain's.
        self.assertLessEqual(crew_words, 260)

    def test_crew_memory_block_keeps_only_the_four_essential_commands(self):
        crew_text = agents.agent_instructions(self.directory, "crew member Jack")
        self.assertIn("CAPTAIN memory show", crew_text)
        self.assertIn("CAPTAIN memory add", crew_text)
        self.assertIn("CAPTAIN memory query", crew_text)
        self.assertIn("CAPTAIN memory path", crew_text)
        # Captain-only content should not leak into the crew block.
        self.assertNotIn("Do not store secrets", crew_text)
        self.assertNotIn("Commit and PR attribution", crew_text)

    def test_captain_instructions_keep_the_full_memory_ruleset(self):
        captain_text = agents.agent_instructions(self.directory, "Captain Barbossa")
        self.assertIn("Do not store secrets", captain_text)
        self.assertIn("Commit and PR attribution", captain_text)

    def test_graphify_query_is_not_advertised_as_always_available(self):
        for role in ("crew member Jack", "Captain Barbossa"):
            text = agents.agent_instructions(self.directory, role)
            query_line = next(line for line in text.splitlines() if "memory query" in line)
            self.assertIn("if Graphify is installed", query_line)


class WaitCrewBlockedTailTests(unittest.TestCase):
    """Regression tests for wait_crew showing the pane tail on a blocked status."""

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

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def wait_api(self, tail, report):
        def api(*call, **kwargs):
            if call[:2] == ("agent", "get"):
                memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                return {"agent": {"agent_status": "blocked"}}
            if call[:2] == ("agent", "read"):
                return tail
            raise AssertionError(f"unexpected herdr call: {call}")

        return api

    def edges(self, relation):
        graph = memory.read_json(self.directory / "graph.json")
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        return [
            labels[link["target"]]
            for link in graph["links"]
            if link["relation"] == relation and labels[link["source"]] == "Jack"
        ]

    def test_blocked_with_a_report_still_prints_the_pane_tail(self):
        with (
            patch.object(
                agents, "herdr", side_effect=self.wait_api("needs approval", "tests pass")
            ),
            patch.object(agents.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "60"), self.pane, self.project)
        printed = output.getvalue()
        self.assertIn("reported: tests pass", printed)
        self.assertIn("pane tail: needs approval", printed)
        # The tail is shown to the captain but not persisted, since a report already was.
        self.assertEqual(self.edges("tail"), [])
        self.assertEqual(self.edges("completed"), ["blocked; reported: tests pass"])


if __name__ == "__main__":
    unittest.main()
