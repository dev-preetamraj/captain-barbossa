"""Tests for query output truncation and label handling."""

import json
import tempfile
import unittest
from pathlib import Path

from captain_barbossa.memory import (
    add_memory,
    empty_graph,
    memory_snapshot,
    note_name,
    read_json,
    truncate_label,
)


class TruncateLabelTests(unittest.TestCase):
    def test_truncate_label_short(self):
        """Short labels should not be modified."""
        short = "This is a short label"
        self.assertEqual(truncate_label(short), short)
        self.assertEqual(truncate_label(short, maxlen=300), short)

    def test_truncate_label_at_boundary(self):
        """Labels at exactly maxlen should not be truncated."""
        label = "x" * 300
        self.assertEqual(truncate_label(label, maxlen=300), label)
        self.assertEqual(len(truncate_label(label, maxlen=300)), 300)

    def test_truncate_label_over_limit(self):
        """Labels over maxlen should be truncated with ellipsis."""
        long_label = "x" * 350
        truncated = truncate_label(long_label, maxlen=300)
        self.assertEqual(len(truncated), 300)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated, "x" * 297 + "...")

    def test_truncate_label_custom_maxlen(self):
        """Custom maxlen should be respected."""
        long_label = "abcdefghij" * 20  # 200 chars
        truncated = truncate_label(long_label, maxlen=50)
        self.assertEqual(len(truncated), 50)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated[:47], long_label[:47])


class AddMemoryTests(unittest.TestCase):
    def test_add_memory_caps_long_labels(self):
        """Long labels should be capped in the graph and spilled to notes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "graph.json"
            long_subject = "x" * 500
            long_target = "y" * 500

            add_memory(path, long_subject, "relation", long_target)

            graph = read_json(path)
            labels = {node["id"]: node["label"] for node in graph["nodes"]}

            for full in (long_subject, long_target):
                marker = f" see notes/{note_name(full)}"
                capped = truncate_label(full, 300 - len(marker)) + marker
                self.assertIn(capped, labels.values())
                self.assertEqual(
                    (path.parent / "notes" / note_name(full)).read_text(encoding="utf-8"), full
                )


class MemorySnapshotTests(unittest.TestCase):
    def test_memory_snapshot_truncates_labels(self):
        """Labels in snapshot should be truncated for graphify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()

            # Create a graph with long labels
            long_label = "x" * 350
            graph = empty_graph()
            graph["nodes"].append({"id": "node1", "label": long_label, "file_type": "memory"})
            graph["nodes"].append({"id": "node2", "label": "short", "file_type": "memory"})
            graph["links"].append(
                {
                    "source": "node1",
                    "target": "node2",
                    "key": "relation",
                    "relation": "related",
                    "confidence": 1.0,
                }
            )

            session_graph = session_dir / "graph.json"
            with open(session_graph, "w") as f:
                json.dump(graph, f)

            # Create snapshot and verify labels are truncated
            with memory_snapshot(session_dir) as snapshot:
                snapshot_graph = read_json(snapshot / "graph.json")
                labels = {node["id"]: node["label"] for node in snapshot_graph["nodes"]}

                # Long label should be truncated in snapshot
                self.assertEqual(len(labels["node1"]), 300)
                self.assertTrue(labels["node1"].endswith("..."))
                # Short label unchanged
                self.assertEqual(labels["node2"], "short")

    def test_memory_snapshot_cleans_stray_dirs(self):
        """Stray query-* directories should be cleaned up at snapshot start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()

            # Create a stray query-* directory
            stray_dir = session_dir / "query-stray"
            stray_dir.mkdir()
            (stray_dir / "file.txt").write_text("stray")

            # Create empty session graph
            graph = empty_graph()
            (session_dir / "graph.json").write_text(json.dumps(graph))

            # Snapshot should clean up stray directory
            with memory_snapshot(session_dir) as _snapshot:
                self.assertFalse(stray_dir.exists())

    def test_snapshot_does_not_modify_stored_graph(self):
        """Snapshot truncation should not modify the stored graph.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()

            long_label = "x" * 350
            graph = empty_graph()
            graph["nodes"].append({"id": "node1", "label": long_label, "file_type": "memory"})

            session_graph = session_dir / "graph.json"
            with open(session_graph, "w") as f:
                json.dump(graph, f)

            original_content = session_graph.read_text()

            # Create snapshot
            with memory_snapshot(session_dir):
                pass

            # Original graph should be unchanged
            self.assertEqual(session_graph.read_text(), original_content)
            stored_graph = read_json(session_graph)
            self.assertEqual(stored_graph["nodes"][0]["label"], long_label)
