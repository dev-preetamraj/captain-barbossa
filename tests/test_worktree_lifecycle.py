"""Fresh pinned checkouts retain uncertain or unintegrated crew work."""

import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import execution, lifetime, results, sessions, store, worktrees
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class WorktreeLifecycleTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.git("init")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.project / "file").write_text("base\n")
        self.git("add", "file")
        self.git("commit", "-m", "base")
        sid = uuid.uuid4().hex
        directory = store.state_storage(self.project) / "sessions" / sid
        directory.mkdir(parents=True)
        self.session = sessions.Session(directory, {"id": sid, "project": str(self.project)})
        # The Linux sweep fails closed on any unreadable same-user /proc entry; CI hosts
        # have such unrelated processes, so a test sweeps only the processes it started.
        self.swept = set()
        listing = lifetime._pids
        sweep = patch.object(
            lifetime,
            "_pids",
            side_effect=lambda: [e for e in listing() if int(e.name) in self.swept],
        )
        sweep.start()
        self.addCleanup(sweep.stop)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.project, stderr=subprocess.DEVNULL)

    def reserve(self):
        return worktrees.reserve(self.session, "Elizabeth", uuid.uuid4().hex, uuid.uuid4().hex)

    def verified_lifetime(self):
        verifier = patch("captain_barbossa.lifetime.check", return_value=SimpleNamespace(ok=True))
        check = verifier.start()
        self.addCleanup(verifier.stop)
        return check

    def test_pins_exact_commit_without_copying_dirty_inputs(self):
        record = self.reserve()
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"])
        (self.project / "file").write_text("new head\n")
        self.git("commit", "-am", "advance")
        (self.project / "dirty").write_text("private")
        created = worktrees.create(self.session, record["id"])
        root = Path(created["execution_root"])
        self.assertEqual((root / "file").read_text(), "base\n")
        self.assertFalse((root / "dirty").exists())
        self.assertEqual(
            worktrees.git(root, "rev-parse", "HEAD").decode().strip(), record["base_oid"]
        )
        self.assertEqual(worktrees.git(root, "branch", "--show-current"), b"")
        second = worktrees.create(self.session, self.reserve()["id"])
        self.assertNotEqual(second["execution_root"], created["execution_root"])

    def test_creation_failure_keeps_intent(self):
        record = self.reserve()
        with patch.object(worktrees, "git", side_effect=CaptainError("interrupted")):
            with self.assertRaises(CaptainError):
                worktrees.create(self.session, record["id"])
        retained = worktrees.read(self.session, record["id"], validate=False)
        self.assertEqual(retained["phase"], "creating")
        self.assertIn("interrupted", retained["failure_reason"])

    def test_validation_pointer_reads_refuse_nonregular_and_oversized_files(self):
        record = worktrees.create(self.session, self.reserve()["id"])
        paths = [
            Path(record["execution_root"]) / ".git",
            Path(record["worktree_git_dir"]) / "gitdir",
            Path(record["worktree_git_dir"]) / "commondir",
        ]
        for pointer in paths:
            original = pointer.read_bytes()
            for kind in ("fifo", "symlink", "directory", "oversized"):
                with self.subTest(pointer=pointer.name, kind=kind):
                    pointer.unlink()
                    if kind == "fifo":
                        os.mkfifo(pointer)
                    elif kind == "symlink":
                        pointer.symlink_to(self.project / "file")
                    elif kind == "directory":
                        pointer.mkdir()
                    else:
                        pointer.write_bytes(b"x" * 4097)
                    try:
                        with self.assertRaises(CaptainError):
                            worktrees.validate_registration(record)
                    finally:
                        if kind == "directory":
                            pointer.rmdir()
                        else:
                            pointer.unlink()
                        pointer.write_bytes(original)

    def test_validation_shares_inspection_deadline_and_entry_limit_without_git(self):
        record = worktrees.create(self.session, self.reserve()["id"])
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"], deadline=time.monotonic() - 1)
        with patch("captain_barbossa.inspection.MAX_ENTRIES", 1):
            with self.assertRaises(CaptainError):
                worktrees.validate_registration(record)
        with patch.object(worktrees, "git", side_effect=AssertionError("validation invoked Git")):
            worktrees.validate_registration(record)

    def test_oversized_or_fifo_identity_record_fails_closed(self):
        record = self.reserve()
        path = worktrees.record_path(self.session, record["id"])
        path.write_bytes(b" " * 65537)
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"], validate=False)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"], validate=False)

    def test_creation_does_not_execute_checkout_hooks(self):
        hook = self.project / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 77\n")
        hook.chmod(0o755)
        record = worktrees.create(self.session, self.reserve()["id"])
        self.assertEqual(record["phase"], "ready")

    def test_unknown_special_file_blocks_even_explicit_discard_cleanup(self):
        self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        os.mkfifo(Path(record["execution_root"]) / "pipe")
        manifest = results.collect(self.session, record["id"], ["."])
        self.assertTrue(manifest["unresolved"])
        results.discard(self.session, manifest["id"], "discard requested")
        with self.assertRaises(CaptainError):
            worktrees.dispose(self.session, record["id"])

    def test_rejects_stale_record_and_forged_reciprocal_pointer(self):
        record = worktrees.create(self.session, self.reserve()["id"])
        pointer = Path(record["worktree_git_dir"]) / "gitdir"
        pointer.write_text(str(self.project / ".git"))
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"])
        record["assignment_id"] = "invalid"
        store.write_json(worktrees.record_path(self.session, record["id"]), record)
        with self.assertRaises(CaptainError):
            worktrees.read(self.session, record["id"], validate=False)

    def test_provisional_results_cannot_authorize_cleanup(self):
        record = worktrees.create(self.session, self.reserve()["id"])
        results.collect(self.session, record["id"], ["."])
        with self.assertRaises(CaptainError):
            worktrees.dispose(self.session, record["id"])
        self.assertTrue(Path(record["execution_root"]).exists())

    def test_empty_final_result_can_be_removed_and_journal_is_retained(self):
        check = self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        manifest = results.collect(self.session, record["id"], ["."])
        removed = worktrees.dispose(self.session, record["id"])
        self.assertEqual(removed["phase"], "removed")
        self.assertFalse(Path(record["execution_root"]).exists())
        self.assertEqual(results.read(self.session, manifest["id"]), manifest)
        self.assertEqual(worktrees.inventory(self.session)[0]["phase"], "removed")
        self.assertGreaterEqual(check.call_count, 2)
        self.assertEqual(
            check.call_args.kwargs,
            {
                "crew_id": record["crew_id"],
                "incarnation_id": record["incarnation_id"],
                "root": record["execution_root"],
                "worktree": record["id"],
            },
        )

    def test_missing_lifetime_does_not_finalize_collection(self):
        record = worktrees.create(self.session, self.reserve()["id"])
        manifest = results.collect(self.session, record["id"], ["."])
        self.assertTrue(manifest["provisional"])

    def test_disposal_rechecks_authoritative_lifetime_after_collection(self):
        check = self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        manifest = results.collect(self.session, record["id"], ["."])
        self.assertFalse(manifest["provisional"])
        check.return_value = SimpleNamespace(ok=False)
        with self.assertRaises(CaptainError):
            worktrees.dispose(self.session, record["id"])
        self.assertTrue(Path(record["execution_root"]).exists())

    def test_post_collection_edit_blocks_cleanup_even_after_discard(self):
        self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        manifest = results.collect(self.session, record["id"], ["."])
        results.discard(self.session, manifest["id"], "user explicitly discarded")
        (Path(record["execution_root"]) / "new").write_text("later")
        with self.assertRaises(CaptainError):
            worktrees.dispose(self.session, record["id"])

    def test_interrupted_remove_is_reconciled_without_repeating_deletion(self):
        self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        results.collect(self.session, record["id"], ["."])
        original = worktrees.durable

        def interrupt(path, value):
            if value.get("phase") == "removed":
                raise OSError("crash after git remove")
            original(path, value)

        with patch.object(worktrees, "durable", side_effect=interrupt):
            with self.assertRaises(OSError):
                worktrees.dispose(self.session, record["id"])
        self.assertEqual(
            worktrees.read(self.session, record["id"], validate=False)["phase"], "removing"
        )
        self.assertEqual(worktrees.dispose(self.session, record["id"])["phase"], "removed")

    def test_dismissal_keeps_settled_final_result_and_cleans_up(self):
        self.verified_lifetime()
        for settle in ("apply", "discard"):
            with self.subTest(settle=settle):
                record = worktrees.create(self.session, self.reserve()["id"])
                (Path(record["execution_root"]) / settle).write_text("crew work\n")
                manifest = results.collect(self.session, record["id"], ["."])
                self.assertFalse(manifest["provisional"])
                if settle == "apply":
                    results.apply(self.session, manifest["id"])
                else:
                    results.discard(self.session, manifest["id"], "user discarded")
                crew = SimpleNamespace(
                    is_background=False,
                    session=self.session,
                    record={"lifetime_id": record["incarnation_id"], "worktree_id": record["id"]},
                )
                with (
                    patch.object(
                        execution, "stop", return_value=execution.lifetime.Verdict(True, "stopped")
                    ),
                    patch.object(execution.native_history, "cleanup", return_value={}),
                    patch.object(
                        execution.protocol, "active_assignment", return_value={"paths": ["."]}
                    ),
                ):
                    state = execution.retire(crew)
                self.assertEqual(state["result_id"], manifest["id"])
                self.assertEqual(state["worktree"], "removed")
                collected = [
                    item["id"]
                    for item in results.inventory(self.session)
                    if item["worktree_id"] == record["id"]
                ]
                self.assertEqual(collected, [manifest["id"]])
                self.assertFalse(Path(record["execution_root"]).exists())

    def retire(self, record):
        crew = SimpleNamespace(
            is_background=False,
            session=self.session,
            record={"lifetime_id": record["incarnation_id"], "worktree_id": record["id"]},
        )
        with (
            patch.object(execution, "stop", return_value=execution.lifetime.Verdict(True, "ok")),
            patch.object(execution.native_history, "cleanup", return_value={}),
            patch.object(execution.protocol, "active_assignment", return_value={"paths": ["."]}),
        ):
            return execution.retire(crew)

    def test_process_working_inside_worktree_retains_it_alive(self):
        self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        results.collect(self.session, record["id"], ["."])
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=record["execution_root"]
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.swept.add(holder.pid)
        state = self.retire(record)
        self.assertIn(str(holder.pid), state["worktree"])
        self.assertTrue(state["worktree"].startswith("retained:"))
        self.assertIsNone(holder.poll())
        self.assertTrue(Path(record["execution_root"]).exists())
        with patch.object(execution.lifetime, "cwd_holders", side_effect=CaptainError("no lsof")):
            self.assertEqual(self.retire(record)["worktree"], "retained: no lsof")

    def test_leftover_after_removal_is_a_failure_not_clean_removal(self):
        self.verified_lifetime()
        record = worktrees.create(self.session, self.reserve()["id"])
        results.collect(self.session, record["id"], ["."])
        git = worktrees.git

        def remove_nothing(root, *args):
            return b"" if args[:2] == ("worktree", "remove") else git(root, *args)

        with patch.object(worktrees, "git", side_effect=remove_nothing):
            state = self.retire(record)
        self.assertIn("removal incomplete", state["worktree"])
        self.assertEqual(
            worktrees.read(self.session, record["id"], validate=False)["phase"], "removing"
        )


if __name__ == "__main__":
    unittest.main()
