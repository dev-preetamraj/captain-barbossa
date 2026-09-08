"""Graph labels stay short; the full text spills to a session-local note file."""

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from captain_barbossa import memory


class TruncateLabelTests(unittest.TestCase):
    def test_truncate_label_short(self):
        short = "This is a short label"
        self.assertEqual(memory.truncate_label(short), short)
        self.assertEqual(memory.truncate_label(short, maxlen=300), short)

    def test_truncate_label_at_boundary(self):
        label = "x" * 300
        self.assertEqual(memory.truncate_label(label, maxlen=300), label)
        self.assertEqual(len(memory.truncate_label(label, maxlen=300)), 300)

    def test_truncate_label_over_limit(self):
        long_label = "x" * 350
        truncated = memory.truncate_label(long_label, maxlen=300)
        self.assertEqual(len(truncated), 300)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated, "x" * 297 + "...")

    def test_truncate_label_custom_maxlen(self):
        long_label = "abcdefghij" * 20
        truncated = memory.truncate_label(long_label, maxlen=50)
        self.assertEqual(len(truncated), 50)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated[:47], long_label[:47])


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

    def test_long_relation_is_capped_and_spilled_like_a_label(self):
        relation = "reported " + "r" * 5000
        memory.add_memory(self.path, "Jack", relation, "done")
        [link] = memory.read_json(self.path)["links"]
        self.assertEqual(len(link["relation"]), memory.LABEL_LIMIT)
        self.assertEqual(link["key"], link["relation"])
        self.assertTrue(link["relation"].endswith(f"... see notes/{memory.note_name(relation)}"))
        self.assertEqual(
            (self.directory / "notes" / memory.note_name(relation)).read_text(encoding="utf-8"),
            relation,
        )

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


class GraphMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.directory = memory.private_dir(self.root / "sessions" / ("a" * 32))
        self.path = self.directory / "graph.json"

    def legacy_graph(self, path, labels, relation="has"):
        """A graph keyed the old way: sha256("<graph path>:<label>")."""
        ids = [hashlib.sha256(f"{path}:{label}".encode()).hexdigest() for label in labels]
        graph = memory.empty_graph()
        graph["nodes"] = [
            {"id": i, "label": label, "file_type": "memory"}
            for i, label in zip(ids, labels, strict=True)
        ]
        graph["links"] = [
            {
                "source": ids[0],
                "target": ids[1],
                "key": relation,
                "relation": relation,
                "confidence": 1.0,
            }
        ]
        memory.write_json(path, graph)
        return graph

    def test_node_ids_do_not_depend_on_the_graph_path(self):
        other = memory.private_dir(self.root / "elsewhere") / "graph.json"
        memory.add_memory(self.path, "Jack", "report", "done")
        memory.add_memory(other, "Jack", "report", "done")
        ids = [{node["id"] for node in memory.read_json(p)["nodes"]} for p in (self.path, other)]
        self.assertEqual(*ids)
        self.assertEqual(ids[0], {memory.node_id("Jack"), memory.node_id("done")})

    def test_legacy_path_keyed_graph_is_rekeyed_on_load(self):
        self.legacy_graph(self.path, ["Jack", "done"])
        graph = memory.load_graph(self.path)
        self.assertEqual(
            [node["id"] for node in graph["nodes"]],
            [memory.node_id("Jack"), memory.node_id("done")],
        )
        [link] = graph["links"]
        self.assertEqual(
            (link["source"], link["target"]), (memory.node_id("Jack"), memory.node_id("done"))
        )
        self.assertEqual(memory.read_json(self.path), graph)  # rewritten to disk
        self.assertFalse(memory.migrate_graph(self.path, memory.read_json(self.path)))

    def test_legacy_long_label_spills_to_a_note_on_load(self):
        report = "old report " + "o" * 4000
        self.legacy_graph(self.path, ["Jack", report], relation="report")
        graph = memory.load_graph(self.path)
        [label] = [node["label"] for node in graph["nodes"] if node["label"] != "Jack"]
        name = memory.note_name(report)
        self.assertEqual(len(label), memory.LABEL_LIMIT)
        self.assertTrue(label.endswith(f"... see notes/{name}"))
        self.assertEqual((self.directory / "notes" / name).read_text(encoding="utf-8"), report)

    def test_a_dangling_link_is_skipped_not_fatal(self):
        memory.add_memory(self.path, "Jack", "report", "done")
        graph = memory.read_json(self.path)
        graph["links"].append(
            {**graph["links"][0], "source": "missing", "key": "orphan", "relation": "orphan"}
        )
        memory.write_json(self.path, graph)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.show_memory(self.directory, show_all=True)
        rows = output.getvalue().splitlines()[1:]
        self.assertEqual(rows, ['[session] ["Jack", "report", "done"]'])


if __name__ == "__main__":
    unittest.main()
