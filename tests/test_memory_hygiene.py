"""Tests for memory.py hygiene: lock file and session directory permissions."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli
from captain_barbossa.sessions import session
from captain_barbossa.store import lock


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

    def test_lock_file_parent_created_private(self):
        """A freshly created lock parent directory should be 0700, not world/group readable."""
        lock_path = self.temp_dir / "fresh_subdir" / "test.lock"
        with lock(lock_path):
            pass
        mode = stat.S_IMODE(lock_path.parent.stat().st_mode)
        self.assertEqual(mode, 0o700, f"Lock parent dir has mode {oct(mode)}, expected 0700")


class TestAppendEventSecureOpen(unittest.TestCase):
    """append_event must not follow a symlink swapped in at the event log path."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_append_event(self, argv1, event):
        cli.main(["hook", argv1, json.dumps(event)])

    def test_refuses_a_symlinked_event_log(self):
        target = self.temp_dir / "elsewhere.jsonl"
        link = self.temp_dir / "events.jsonl"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            self.run_append_event(str(link), {"hook_event_name": "Notification"})
        self.assertFalse(target.exists())

    def test_creates_the_log_with_0600(self):
        path = self.temp_dir / "events.jsonl"
        self.run_append_event(str(path), {"hook_event_name": "Notification"})
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"Event log has mode {oct(mode)}, expected 0600")
        self.assertIn("Notification", path.read_text(encoding="utf-8"))


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
