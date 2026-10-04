"""The graph itself: scopes, show, snapshot, and concurrent writes."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from captain_barbossa import cli, memory, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase


class GraphMemoryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # nudge_block/nudge read a realistic agent status these ad hoc herdr fakes don't model;
        # a mail doorbell is not what these tests exercise, so give delivery a clean ring by
        # default. test_submit.py covers nudge_block/nudge themselves against real fakes.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_graph_scopes_projects_sessions_and_parallel_relationships(self):
        shared = self.directory.parent.parent / "graph.json"
        memory.add_memory(shared, "project", "uses", "Python")
        memory.add_memory(self.directory / "graph.json", "task", "uses", "secret-session-fact")
        memory.add_memory(shared, "project", "tests_with", "Python")
        other, _ = sessions.session(self.project, self.pane, create=True)
        with memory.memory_snapshot(other) as snapshot:
            graph = store.read_json(snapshot / "graph.json")
        self.assertEqual(len(graph["links"]), 2)
        self.assertNotIn("secret-session-fact", json.dumps(graph))
        project2 = self.root / "other-project"
        project2.mkdir()
        isolated, _ = sessions.session(project2, self.pane, create=True)
        with memory.memory_snapshot(isolated) as snapshot:
            self.assertEqual(store.read_json(snapshot / "graph.json")["nodes"], [])
        self.assertEqual(list(isolated.glob("query-*")), [])
        with self.assertRaisesRegex(runtime.CaptainError, "another project or Herdr workspace"):
            sessions.session(self.project, dict(self.pane, workspace_id="w2"), self.meta["id"])

    def test_memory_show_preserves_relationships_and_offers_raw_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show"), self.pane, self.project)
        self.assertEqual(output.getvalue(), "Memory (subject, relation, object):\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show", "--json"), self.pane, self.project)
        self.assertEqual(json.loads(output.getvalue()), memory.empty_graph())

        shared = self.directory.parent.parent / "graph.json"
        local = self.directory / "graph.json"
        facts = [
            ("project", "uses", "Python"),
            ("project", "tests_with", "Python"),
            ("project", "uses", 'quoted "fact"\nwith tabs\tand Unicode: café → ✅'),
            ("long", "keeps", "x" * 8000),
        ]
        for path, fact in zip((shared, shared, local, local), facts):
            memory.add_memory(path, *fact)
        before = {path: path.read_bytes() for path in (shared, local)}
        with memory.memory_snapshot(self.directory) as snapshot:
            raw = (snapshot / "graph.json").read_text()

        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show"), self.pane, self.project)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "Memory (subject, relation, object):")
        # session links newest-first, then project links newest-first
        expected_scopes = ["session", "session", "project", "project"]
        expected_facts = [facts[3], facts[2], facts[1], facts[0]]
        printed_scopes = []
        printed_facts = []
        for line in lines[1:]:
            tag, _, rest = line.partition(" ")
            printed_scopes.append(tag.strip("[]"))
            printed_facts.append(tuple(json.loads(rest)))
        self.assertEqual(printed_scopes, expected_scopes)
        self.assertEqual(printed_facts[1:], expected_facts[1:])
        capped = printed_facts[0][2]
        self.assertEqual(printed_facts[0][:2], facts[3][:2])
        self.assertEqual(len(capped), memory.LABEL_LIMIT)
        note = memory.note_name(facts[3][2])
        self.assertTrue(capped.endswith(f" see notes/{note}"))
        self.assertEqual((self.directory / "notes" / note).read_text(encoding="utf-8"), facts[3][2])
        stored = store.read_json(local)
        stored_labels = {node["id"]: node["label"] for node in stored["nodes"]}
        self.assertIn(capped, stored_labels.values())
        self.assertLess(len(output.getvalue()), len(raw))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(self.args("memory", "show", "--json"), self.pane, self.project)
        self.assertEqual(output.getvalue(), raw)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(list(self.directory.glob("query-*")), [])
        self.assertEqual(list(self.project.iterdir()), [])

    def test_failed_snapshot_write_removes_the_query_directory(self):
        memory.add_memory(self.directory / "graph.json", "task", "has", "fact")
        for fail in (
            patch.object(memory, "write_json", side_effect=OSError("No space left on device")),
            patch.object(memory, "private_dir", side_effect=runtime.CaptainError("not private")),
        ):
            with self.subTest(fail=fail.attribute), fail:
                # Showing memory is now a pure read, even when writes are unavailable.
                memory.memory(self.args("memory", "show"), self.pane, self.project)
                with self.assertRaises((OSError, runtime.CaptainError)):
                    with memory.memory_snapshot(self.directory):
                        self.fail("snapshot should not be yielded")
            self.assertEqual(list(self.directory.glob("query-*")), [])
        with (
            patch.object(memory, "executable", return_value="/bin/graphify"),
            patch.object(memory.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "status 3"):
                memory.memory(self.args("memory", "query", "fact"), self.pane, self.project)
        self.assertEqual(list(self.directory.glob("query-*")), [])
        self.assertEqual(list(self.directory.glob(".captain-*")), [])

    def test_graph_concurrent_writes_and_corruption_preservation(self):
        graph_path = self.directory / "graph.json"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(lambda i: memory.add_memory(graph_path, "task", "has", str(i)), range(20))
            )
        self.assertEqual(len(store.read_json(graph_path)["links"]), 20)
        graph_path.write_text("broken json")
        with self.assertRaisesRegex(runtime.CaptainError, "Cannot read memory"):
            memory.add_memory(graph_path, "x", "y", "z")
        self.assertEqual(graph_path.read_text(), "broken json")

    def test_memory_rejects_repo_storage_and_invalid_session_paths(self):
        with patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.project / ".memory")}):
            with self.assertRaisesRegex(runtime.CaptainError, "outside the project"):
                store.storage(self.project)
        with self.assertRaisesRegex(runtime.CaptainError, "Invalid captain session"):
            sessions.session(self.project, self.pane, "../../elsewhere")
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(shutil.which("graphify"), "Graphify is optional")
    def test_real_graphify_reads_memory_without_writing_in_project(self):
        memory.add_memory(self.directory / "graph.json", "rate limiter", "uses", "per-user windows")
        with memory.memory_snapshot(self.directory) as snapshot:
            env = dict(os.environ, GRAPHIFY_OUT=str(snapshot), GRAPHIFY_QUERY_LOG_DISABLE="1")
            result = subprocess.run(
                [
                    runtime.executable("graphify"),
                    "query",
                    "rate limiter",
                    "--graph",
                    str(snapshot / "graph.json"),
                ],
                cwd=snapshot,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("per-user windows", result.stdout)
        self.assertEqual(list(self.project.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
