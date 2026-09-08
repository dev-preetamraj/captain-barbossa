"""Graph labels stay short; the full text spills to a session-local note file."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from captain_barbossa import memory


class ShortLabelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.directory = memory.private_dir(self.root / "sessions" / ("a" * 32))
        self.path = self.directory / "graph.json"

    def labels(self, path=None):
        graph = memory.read_json(path or self.path)
        return [node["label"] for node in graph["nodes"]]

    def test_short_labels_are_stored_verbatim_without_notes(self):
        memory.add_memory(self.path, "Jack", "report", "x" * memory.LABEL_LIMIT)
        self.assertIn("x" * memory.LABEL_LIMIT, self.labels())
        self.assertFalse((self.directory / "notes").exists())

    def test_long_object_is_capped_and_spilled_to_a_note(self):
        report = "tests pass. " + "y" * 5000
        memory.add_memory(self.path, "Jack", "report", report)
        name = memory.note_name(report)
        [label] = [text for text in self.labels() if text != "Jack"]
        self.assertEqual(len(label), memory.LABEL_LIMIT)
        self.assertTrue(label.startswith("tests pass. "))
        self.assertTrue(label.endswith(f"... see notes/{name}"))
        note = self.directory / "notes" / name
        self.assertEqual(note.read_text(encoding="utf-8"), report)
        self.assertEqual(note.stat().st_mode & 0o777, 0o600)

    def test_long_subject_is_capped_too(self):
        memory.add_memory(self.path, "z" * 900, "relation", "short")
        self.assertTrue(all(len(label) <= memory.LABEL_LIMIT for label in self.labels()))

    def test_distinct_long_values_stay_distinct_after_a_shared_prefix(self):
        prefix = "same start " * 40
        memory.add_memory(self.path, "Jack", "report", prefix + "first ending")
        memory.add_memory(self.path, "Jack", "report", prefix + "second ending")
        graph = memory.read_json(self.path)
        self.assertEqual(len({node["id"] for node in graph["nodes"]}), 3)
        self.assertEqual(len(graph["links"]), 2)
        self.assertEqual(len(list((self.directory / "notes").iterdir())), 2)

    def test_repeated_writes_reuse_one_note(self):
        report = "w" * 4000
        memory.add_memory(self.path, "Jack", "report", report)
        memory.add_memory(self.path, "Will", "report", report)
        self.assertEqual(len(list((self.directory / "notes").iterdir())), 1)

    def test_show_and_raw_json_keep_their_shape(self):
        memory.add_memory(self.path, "Jack", "report", "q" * 4000)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.show_memory(self.directory, show_all=True)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "Memory (subject, relation, object):")
        scope, _, rest = lines[1].partition(" ")
        self.assertEqual(scope, "[session]")
        subject, relation, target = json.loads(rest)
        self.assertEqual((subject, relation), ("Jack", "report"))
        self.assertEqual(len(target), memory.LABEL_LIMIT)
        with memory.memory_snapshot(self.directory) as snapshot:
            graph = memory.read_json(snapshot / "graph.json")
        self.assertEqual(set(graph), set(memory.empty_graph()))
        self.assertEqual(sorted(self.labels()), sorted(node["label"] for node in graph["nodes"]))


if __name__ == "__main__":
    unittest.main()
