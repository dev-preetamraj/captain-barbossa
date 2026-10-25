"""Result transfers preserve unrelated dirt and leave uncertain writes journaled."""

import base64
import os
import subprocess
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import results, sessions, store, worktrees
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class ResultTransferTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.git(self.project, "init")
        self.git(self.project, "config", "user.name", "Test")
        self.git(self.project, "config", "user.email", "test@example.invalid")
        for name in ("file", "unrelated", "deleted"):
            (self.project / name).write_text("base\n")
        self.git(self.project, "add", ".")
        self.git(self.project, "commit", "-m", "base")
        sid = uuid.uuid4().hex
        directory = store.state_storage(self.project) / "sessions" / sid
        directory.mkdir(parents=True)
        self.session = sessions.Session(directory, {"id": sid, "project": str(self.project)})
        record = worktrees.reserve(self.session, "Elizabeth", uuid.uuid4().hex, uuid.uuid4().hex)
        self.record = worktrees.create(self.session, record["id"])
        self.checkout = Path(self.record["execution_root"])

    def git(self, root, *args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

    def collect(self, owned=None):
        return results.collect(self.session, self.record["id"], owned or ["."])

    def test_committed_tree_filters_paths_and_preserves_destination_error(self):
        tree = results._tree(self.project, "HEAD")
        self.assertEqual(results._committed(self.project, ["file"]), {"file": tree["file"]})
        (self.project / "link").symlink_to("file")
        self.git(self.project, "add", "link")
        self.git(self.project, "commit", "-m", "link")
        with self.assertRaisesRegex(CaptainError, "transfer-unsupported.*Destination HEAD"):
            results._committed(self.project, ["link"])
        self.assertEqual(results._committed(self.project, ["file"]), {"file": tree["file"]})

    def test_tracked_file_replaced_by_directory_transfers_deletion_and_children(self):
        (self.checkout / "file").unlink()
        (self.checkout / "file").mkdir()
        (self.checkout / "file/new").write_text("child\n")
        manifest = self.collect(["file"])
        self.assertEqual(manifest["unresolved"], [])
        entries = {entry["path"]: entry for entry in manifest["entries"]}
        self.assertEqual(set(entries), {"file", "file/new"})
        self.assertIsNone(entries["file"]["post"])
        self.assertIsNone(entries["file/new"]["base"])
        index = (self.project / ".git/index").read_bytes()
        receipt = results.apply(self.session, manifest["id"])
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual((self.project / "file/new").read_text(), "child\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)
        self.assertEqual(results.preview(self.session, manifest["id"])["observed_state"], "applied")
        self.assertEqual(results.apply(self.session, manifest["id"]), receipt)

    def test_file_to_directory_apply_allows_verified_cleanup(self):
        (self.checkout / "file").unlink()
        (self.checkout / "file").mkdir()
        (self.checkout / "file/new").write_text("child\n")
        self.git(self.checkout, "add", "file")
        with patch("captain_barbossa.lifetime.check", return_value=SimpleNamespace(ok=True)):
            manifest = self.collect(["file"])
            self.assertEqual(results.apply(self.session, manifest["id"])["state"], "applied")
            worktrees.dispose(self.session, self.record["id"])
        self.assertFalse(self.checkout.exists())
        self.assertEqual((self.project / "file/new").read_text(), "child\n")
        self.assertEqual(results.read(self.session, manifest["id"]), manifest)

    def test_collects_committed_staged_unstaged_binary_modes_and_untracked(self):
        (self.checkout / "file").write_text("committed\n")
        self.git(self.checkout, "commit", "-am", "crew commit")
        (self.checkout / "file").write_text("staged\n")
        self.git(self.checkout, "add", "file")
        (self.checkout / "file").write_bytes(b"unstaged\x00binary")
        (self.checkout / "file").chmod(0o755)
        (self.checkout / "new").write_bytes(b"new\x00binary")
        (self.checkout / "deleted").unlink()
        index_path = Path(self.record["worktree_git_dir"]) / "index"
        before = index_path.read_bytes()
        manifest = self.collect()
        self.assertEqual(index_path.read_bytes(), before)
        self.assertNotIn("patch", manifest)
        entries = {entry["path"]: entry for entry in manifest["entries"]}
        self.assertEqual(
            base64.b64decode(entries["file"]["post"]["content"]), b"unstaged\x00binary"
        )
        self.assertEqual(entries["file"]["post"]["mode"], "100755")
        self.assertIsNone(entries["deleted"]["post"])
        self.assertIn(base64.b64encode(b"staged\n").decode(), manifest["index_blobs"].values())
        self.assertEqual(
            base64.b64decode(manifest["head_files"]["file"]["content"]), b"committed\n"
        )
        self.assertEqual(results.read(self.session, manifest["id"]), manifest)

    def assert_untouched(self, manifest, index):
        self.assertIsNone(results.preview(self.session, manifest["id"])["transfer"])
        self.assertEqual((self.project / ".git/index").read_bytes(), index)
        self.assertFalse((self.project / "new").exists())
        self.assertTrue((self.project / "deleted").exists())

    def test_apply_writes_postimages_unstaged_and_preserves_unrelated_work(self):
        (self.checkout / "file").write_text("crew\n")
        (self.checkout / "new").write_bytes(b"\x00payload")
        (self.checkout / "new").chmod(0o755)
        (self.checkout / "dir").mkdir()
        (self.checkout / "dir/nested").write_text("nested\n")
        (self.checkout / "deleted").unlink()
        manifest = self.collect()
        (self.project / "unrelated").write_text("staged user\n")
        self.git(self.project, "add", "unrelated")
        (self.project / "unrelated").write_text("unstaged user\n")
        index = (self.project / ".git/index").read_bytes()
        receipt = results.apply(self.session, manifest["id"])
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual((self.project / "file").read_text(), "crew\n")
        self.assertEqual((self.project / "new").read_bytes(), b"\x00payload")
        self.assertTrue(os.access(self.project / "new", os.X_OK))
        self.assertEqual((self.project / "dir/nested").read_text(), "nested\n")
        self.assertFalse((self.project / "deleted").exists())
        self.assertEqual((self.project / "unrelated").read_text(), "unstaged user\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)
        staged = self.git(self.project, "diff", "--cached", "--name-only").decode().split()
        self.assertEqual(staged, ["unrelated"])
        self.assertEqual(results.preview(self.session, manifest["id"])["observed_state"], "applied")
        self.assertEqual(results.apply(self.session, manifest["id"]), receipt)

    def test_working_file_off_its_base_preimage_refuses_and_records_nothing(self):
        (self.checkout / "file").write_text("crew")
        (self.checkout / "new").write_text("crew")
        (self.checkout / "deleted").unlink()
        manifest = self.collect()
        index = (self.project / ".git/index").read_bytes()
        (self.project / "file").write_text("user")
        with self.assertRaisesRegex(CaptainError, r"transfer-overlap.*file; nothing applied"):
            results.apply(self.session, manifest["id"])
        self.assert_untouched(manifest, index)
        self.assertEqual((self.project / "file").read_text(), "user")
        (self.project / "file").write_text("base\n")
        (self.project / "new").write_text("untracked user")
        with self.assertRaisesRegex(CaptainError, r"transfer-overlap.*new; nothing applied"):
            results.apply(self.session, manifest["id"])
        self.assertIsNone(results.preview(self.session, manifest["id"])["transfer"])
        self.assertEqual((self.project / "new").read_text(), "untracked user")
        self.assertEqual((self.project / "file").read_text(), "base\n")
        self.assertTrue((self.project / "deleted").exists())

    def test_staged_entry_differing_from_head_refuses_and_records_nothing(self):
        (self.checkout / "file").write_text("crew")
        (self.checkout / "new").write_text("crew")
        (self.checkout / "deleted").unlink()
        manifest = self.collect()
        (self.project / "file").write_text("staged user")
        self.git(self.project, "add", "file")
        (self.project / "file").write_text("base\n")
        index = (self.project / ".git/index").read_bytes()
        with self.assertRaisesRegex(CaptainError, r"Staged changes on touched paths: file"):
            results.apply(self.session, manifest["id"])
        self.assert_untouched(manifest, index)
        self.assertEqual((self.project / "file").read_text(), "base\n")

    def test_symlink_ancestor_and_untracked_collision_refuse(self):
        (self.checkout / "dir").mkdir()
        (self.checkout / "dir/new").write_text("crew")
        manifest = self.collect()
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / "dir").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(CaptainError):
            results.apply(self.session, manifest["id"])
        self.assertFalse((outside / "new").exists())

    def test_interleaved_writer_is_preserved_and_journaled_as_partial(self):
        for name in ("file", "deleted"):
            (self.checkout / name).write_text("crew")
        manifest = self.collect()
        original = worktrees.durable

        def interleave(path, value):
            original(path, value)
            if path.name == "transfer.json" and value["state"] == "applying":
                (self.project / "file").write_text("external in-place edit")

        with patch.object(worktrees, "durable", side_effect=interleave):
            with self.assertRaisesRegex(CaptainError, "transfer-partial"):
                results.apply(self.session, manifest["id"])
        self.assertEqual((self.project / "file").read_text(), "external in-place edit")
        journal = results.preview(self.session, manifest["id"])["transfer"]
        self.assertEqual(journal["state"], "partial")
        self.assertEqual(journal["entries"], manifest["entries"])
        with self.assertRaisesRegex(CaptainError, "Earlier transfer ended partial"):
            results.apply(self.session, manifest["id"])
        self.assertEqual((self.project / "file").read_text(), "external in-place edit")

    def test_edit_during_temp_file_creation_is_preserved_and_journaled_partial(self):
        (self.checkout / "file").write_text("crew")
        manifest = self.collect()
        real = os.fsync
        edited = []

        def racing(fd):
            real(fd)
            if not edited and os.fstat(fd).st_size == len("crew"):
                edited.append(1)
                (self.project / "file").write_text("editor save")

        with patch.object(os, "fsync", side_effect=racing):
            with self.assertRaisesRegex(CaptainError, "transfer-partial"):
                results.apply(self.session, manifest["id"])
        self.assertTrue(edited)
        self.assertEqual((self.project / "file").read_text(), "editor save")
        self.assertEqual(list(self.project.glob(".file.captain-*")), [])
        self.assertEqual(
            results.preview(self.session, manifest["id"])["transfer"]["state"], "partial"
        )

    def test_applied_receipt_allows_verified_cleanup_without_discard(self):
        (self.checkout / "file").write_text("crew")
        (self.checkout / "deleted").unlink()
        with patch("captain_barbossa.lifetime.check", return_value=SimpleNamespace(ok=True)):
            manifest = self.collect()
            self.assertEqual(results.apply(self.session, manifest["id"])["state"], "applied")
            worktrees.dispose(self.session, self.record["id"])
        self.assertFalse(self.checkout.exists())
        self.assertEqual((self.project / "file").read_text(), "crew")
        self.assertEqual(results.read(self.session, manifest["id"]), manifest)

    def test_hand_integrated_result_is_recorded_applied_without_writing(self):
        (self.checkout / "file").write_text("crew")
        manifest = self.collect()
        (self.project / "file").write_text("crew")
        receipt = results.apply(self.session, manifest["id"])
        self.assertEqual(receipt["state"], "applied")
        (self.project / "file").write_text("later edit")
        with self.assertRaisesRegex(CaptainError, "Applied files changed since"):
            results.apply(self.session, manifest["id"])
        self.assertEqual((self.project / "file").read_text(), "later edit")

    def test_ignored_and_out_of_grant_content_remains_unresolved(self):
        (self.checkout / ".gitignore").write_text("ignored\n")
        (self.checkout / "ignored").write_text("private")
        (self.checkout / "file").write_text("crew")
        manifest = self.collect(["file"])
        self.assertEqual(
            {item["reason"] for item in manifest["unresolved"]}, {"ignored", "out-of-grant"}
        )
        with self.assertRaises(CaptainError):
            results.apply(self.session, manifest["id"])

    def test_manifest_corruption_and_path_traversal_fail_closed(self):
        manifest = self.collect()
        path = self.session.directory / "results" / manifest["id"] / "manifest.json"
        manifest["report"] = "tampered"
        store.write_json(path, manifest)
        with self.assertRaises(CaptainError):
            results.read(self.session, manifest["id"])
        for value in ("../outside", "/absolute", ".git/config", "a/../b"):
            with self.assertRaises(CaptainError):
                results.collect(self.session, self.record["id"], [value])


if __name__ == "__main__":
    unittest.main()
