"""Tests for memory.py hygiene: lock file permissions and temp dir cleanup."""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa.memory import STALE_QUERY_SECONDS, lock, memory_snapshot, session


class TestLockFilePermissions(unittest.TestCase):
    """Test that lock files are created with secure 0600 permissions."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lock_file_created_with_0600(self):
        """Lock file should be created with 0600 permissions."""
        lock_path = self.temp_dir / "test.lock"
        with lock(lock_path):
            pass
        file_stat = lock_path.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        self.assertEqual(mode, 0o600, f"Lock file has mode {oct(mode)}, expected 0600")

    def test_lock_file_permissions_fixed_on_reuse(self):
        """Existing lock file permissions should be fixed to 0600."""
        lock_path = self.temp_dir / "test.lock"
        # Create lock file with insecure permissions
        lock_path.touch(mode=0o644)
        insecure_mode = stat.S_IMODE(lock_path.stat().st_mode)
        self.assertNotEqual(insecure_mode, 0o600)
        # Use lock context manager
        with lock(lock_path):
            pass
        # Permissions should now be fixed
        file_stat = lock_path.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        self.assertEqual(mode, 0o600, f"Lock file has mode {oct(mode)}, expected 0600")

    def test_lock_file_parent_created(self):
        """Lock function should create parent directories."""
        lock_path = self.temp_dir / "subdir" / "nested" / "test.lock"
        self.assertFalse(lock_path.parent.exists())
        with lock(lock_path):
            pass
        self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.parent.exists())


class TestMemorySnapshotCleanup(unittest.TestCase):
    """Test that memory_snapshot cleans up stray query-* directories."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        # Create a mock session directory structure
        self.session_dir = self.temp_dir / "sessions" / "test_session"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_stray_query_dirs_cleaned_up(self):
        """Stale stray query-* directories should be cleaned up at snapshot start."""
        # Create stray query directories old enough that no concurrent reader
        # could still own them.
        stray1 = self.session_dir / "query-stray1"
        stray2 = self.session_dir / "query-stray2"
        stray1.mkdir()
        stray2.mkdir()
        (stray1 / "file.txt").write_text("content")
        (stray2 / "file.txt").write_text("content")
        self._age(stray1, STALE_QUERY_SECONDS + 60)
        self._age(stray2, STALE_QUERY_SECONDS + 60)

        self.assertTrue(stray1.exists())
        self.assertTrue(stray2.exists())

        # Use memory_snapshot to trigger cleanup
        with memory_snapshot(self.session_dir) as snapshot:
            self.assertTrue(snapshot.exists())
            # Stray dirs should be cleaned up
            self.assertFalse(stray1.exists(), "Stray query-stray1 should be cleaned up")
            self.assertFalse(stray2.exists(), "Stray query-stray2 should be cleaned up")

        # Snapshot should also be cleaned up after context
        self.assertFalse(snapshot.exists(), "Snapshot directory should be cleaned up after context")

    def test_recent_query_dir_not_swept(self):
        """A fresh query-* dir from a concurrent reader must survive another's sweep."""
        concurrent = self.session_dir / "query-concurrent"
        concurrent.mkdir()
        (concurrent / "graph.json").write_text("{}")

        with memory_snapshot(self.session_dir):
            pass

        self.assertTrue(
            concurrent.exists(), "A recent query-* dir must not be swept by another snapshot"
        )

    def test_cleanup_in_finally_block(self):
        """Cleanup should happen in finally block even if exception occurs."""
        snapshot_dir = None
        try:
            with memory_snapshot(self.session_dir) as snapshot:
                snapshot_dir = snapshot
                self.assertTrue(snapshot_dir.exists())
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Snapshot should still be cleaned up despite exception
        self.assertFalse(
            snapshot_dir.exists(), "Snapshot should be cleaned up even after exception"
        )

    def test_non_query_dirs_not_removed(self):
        """Non-query-* directories should not be removed."""
        other_dir = self.session_dir / "other_directory"
        other_dir.mkdir()
        (other_dir / "file.txt").write_text("content")

        with memory_snapshot(self.session_dir):
            pass

        # Non-query directory should still exist
        self.assertTrue(other_dir.exists(), "Non-query directories should not be removed")
        self.assertTrue((other_dir / "file.txt").exists())

    def test_empty_session_dir_no_error(self):
        """memory_snapshot should handle empty session directory."""
        empty_session = self.temp_dir / "empty_session"
        empty_session.mkdir()

        # Should not raise even if directory is empty
        with memory_snapshot(empty_session) as snapshot:
            self.assertTrue(snapshot.exists())
            # Snapshot should contain valid graph
            graph_file = snapshot / "graph.json"
            self.assertTrue(graph_file.exists())


class TestSessionsDirPermissions(unittest.TestCase):
    """The sessions/ parent directory must be private, not just its children."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.project = self.temp_dir / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.temp_dir / "state")})
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sessions_parent_dir_is_private(self):
        directory, _ = session(self.project, {"workspace_id": "w1"}, create=True)
        mode = stat.S_IMODE(directory.parent.stat().st_mode)
        self.assertEqual(mode, 0o700, f"sessions/ has mode {oct(mode)}, expected 0700")


if __name__ == "__main__":
    unittest.main()
