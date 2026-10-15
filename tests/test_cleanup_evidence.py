"""Cleanup accepts authoritative revoked lifetime receipts, never activity guesses."""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import execution, lifetime, sessions, store
from captain_barbossa.runtime import CaptainError


class CleanupEvidenceTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.session = sessions.session(
            self.project,
            {"workspace_id": "workspace", "pane_id": "captain"},
            session_id="a" * 32,
            create=True,
        )
        self.crew = {
            "id": "will",
            "incarnation_id": "b" * 32,
            "pane": "crew-pane",
            "tab": "tab",
            "terminal_id": "crew-terminal",
        }
        self.session.meta["crew"]["will"] = self.crew
        store.write_json(self.session.meta_path, self.session.meta)
        store.write_json(
            self.session.directory / "captain.json",
            {"pane": "captain", "terminal_id": "captain-terminal"},
        )
        self.ref = lifetime.prepare(self.session, self.crew, ["native"], {}, self.project)

    def verdict(self, ref=None, **overrides):
        identity = {
            "crew_id": "will",
            "incarnation_id": self.ref,
            "root": str(self.project),
            "worktree": None,
        }
        identity.update(overrides)
        return lifetime.check(self.session, self.ref if ref is None else ref, **identity)

    def stopped(self, **overrides):
        path = self.session.directory / "lifetimes" / f"{self.ref}.json"
        record = store.read_json(path)
        record.update(
            state="exited",
            receipt_version=2,
            native_returncode=0,
            shutdown={
                "scope": "managed-process-group",
                "state": "complete",
                "guard_reaped": True,
                "group_absent": True,
                "escape_evidence": "descendant-scan",
                "escape_state": "none-observed",
            },
        )
        record.update(overrides)
        store.write_json(path, record)

    def test_revoke_prevents_first_launch_and_reprepare(self):
        self.assertFalse(self.verdict().ok)
        lifetime.revoke(self.session, self.ref)
        self.assertTrue(self.verdict().ok)
        with self.assertRaises(CaptainError), lifetime.claim(self.session, self.ref):
            self.fail("revoked launch claimed")
        with self.assertRaises(CaptainError):
            lifetime.prepare(self.session, self.crew, ["native"], {}, self.project)

    def test_receipt_requires_exit_revocation_and_owner_release(self):
        with lifetime.claim(self.session, self.ref) as job:
            lifetime.revoke(self.session, self.ref)
            self.assertFalse(job.finish().ok)
            self.stopped()
            self.assertFalse(self.verdict().ok)
        self.assertTrue(self.verdict().ok)
        self.assertFalse(lifetime.pending(self.session.directory))
        with self.assertRaises(CaptainError):
            job.finish()
        with self.assertRaises(CaptainError), lifetime.claim(self.session, self.ref):
            self.fail("replayed launch")

    def test_owner_loss_and_outcome_do_not_prove_exit(self):
        with lifetime.claim(self.session, self.ref):
            pass
        lifetime.revoke(self.session, self.ref)
        store.write_json(self.session.directory / "outcome.json", {"state": "done"})
        self.assertEqual(self.verdict().reason, "exit-unknown")
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_legacy_direct_exit_does_not_authorize_writer_cleanup(self):
        self.stopped(receipt_version=1, returncode=0, shutdown=None)
        lifetime.revoke(self.session, self.ref)
        self.assertEqual(self.verdict().reason, "managed-shutdown-unconfirmed")
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_incomplete_or_escaped_group_receipt_is_retained(self):
        lifetime.revoke(self.session, self.ref)
        for field, value in (
            ("guard_reaped", False),
            ("group_absent", False),
            ("escape_state", "reported"),
            ("scope", "direct-child"),
        ):
            self.stopped()
            path = self.session.directory / "lifetimes" / f"{self.ref}.json"
            record = store.read_json(path)
            record["shutdown"][field] = value
            store.write_json(path, record)
            self.assertFalse(self.verdict().ok)

    def test_forged_evidence_and_mismatched_immutable_identities_rejected(self):
        lifetime.revoke(self.session, self.ref)
        for override in (
            {"crew_id": "jack"},
            {"incarnation_id": "c" * 32},
            {"root": str(self.project / "other")},
            {"worktree": "b" * 32},
        ):
            with self.subTest(override=override):
                self.assertFalse(self.verdict(**override).ok)
        self.assertFalse(self.verdict({"id": self.ref, "state": "exited"}).ok)
        self.assertFalse(self.verdict("c" * 32).ok)

    def test_check_is_read_only_and_reloads_changed_receipt(self):
        lifetime.revoke(self.session, self.ref)
        with (
            patch.object(store, "write_json", side_effect=AssertionError("write")),
            patch.object(store, "lock", side_effect=AssertionError("create lock")),
        ):
            self.assertTrue(self.verdict().ok)
        path = self.session.directory / "lifetimes" / f"{self.ref}.json"
        record = store.read_json(path)
        record["state"] = "claimed"
        store.write_json(path, record)
        self.assertFalse(self.verdict().ok)
        path.write_text("broken")
        self.assertFalse(self.verdict().ok)
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_close_failure_revokes_but_never_proves_exit(self):
        lifetime.bind(self.session, self.ref, self.crew)
        self.stopped()
        with patch.object(lifetime.runtime, "herdr", side_effect=CaptainError("unavailable")):
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
        self.assertTrue(lifetime.read(self.session, self.ref)["revoked"])
        self.assertFalse(self.verdict().ok)
        with patch.object(lifetime.runtime, "herdr", side_effect=CaptainError("pane gone")):
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
        self.assertFalse(self.verdict().ok)

    def test_successful_close_waits_for_recorded_exit_but_is_bounded(self):
        lifetime.bind(self.session, self.ref, self.crew)
        with lifetime.claim(self.session, self.ref):
            pass
        pane = lifetime.read(self.session, self.ref)["pane"]
        with (
            patch.object(lifetime.runtime, "herdr", return_value={"pane": pane}),
            patch.object(lifetime.time, "monotonic", side_effect=[0, 1, 100]),
            patch.object(lifetime.time, "sleep") as sleep,
        ):
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
            sleep.assert_called_once_with(lifetime.config.number("lifetime", "poll_interval"))
        self.assertEqual(lifetime.read(self.session, self.ref)["close_state"], "failed")
        self.assertFalse(self.verdict().ok)

    def test_close_observes_receipt_arriving_during_wait(self):
        lifetime.bind(self.session, self.ref, self.crew)
        owner = lifetime.claim(self.session, self.ref)
        owner.__enter__()
        self.addCleanup(owner.__exit__, None, None, None)

        def exited(_delay):
            self.stopped()
            owner.__exit__(None, None, None)

        pane = lifetime.read(self.session, self.ref)["pane"]
        with (
            patch.object(lifetime.runtime, "herdr", return_value={"pane": pane}) as herdr,
            patch.object(lifetime.time, "sleep", side_effect=exited),
        ):
            self.assertTrue(lifetime.close(self.session, self.ref).ok)
            self.assertEqual(herdr.call_count, 2)

    def test_close_rejects_recycled_terminal_and_captain(self):
        lifetime.bind(self.session, self.ref, self.crew)
        pane = {
            "workspace_id": "workspace",
            "tab_id": "tab",
            "pane_id": "crew-pane",
            "terminal_id": "replacement",
        }
        with patch.object(lifetime.runtime, "herdr", return_value={"pane": pane}) as herdr:
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
            self.assertEqual(herdr.call_count, 1)
        store.write_json(
            self.session.directory / "captain.json",
            {"pane": "crew-pane", "terminal_id": "crew-terminal"},
        )
        with patch.object(lifetime.runtime, "herdr") as herdr:
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
            herdr.assert_not_called()

    def test_close_exact_binding_and_stale_roster(self):
        lifetime.bind(self.session, self.ref, self.crew)
        pane = lifetime.read(self.session, self.ref)["pane"]
        self.session.meta["crew"]["will"]["incarnation_id"] = "c" * 32
        store.write_json(self.session.meta_path, self.session.meta)
        with patch.object(lifetime.runtime, "herdr") as herdr:
            self.assertFalse(lifetime.close(self.session, self.ref).ok)
            herdr.assert_not_called()
        self.session.meta["crew"]["will"]["incarnation_id"] = self.ref
        store.write_json(self.session.meta_path, self.session.meta)
        with patch.object(lifetime.runtime, "herdr", return_value={"pane": pane}) as herdr:
            self.assertTrue(lifetime.close(self.session, self.ref).ok)
            self.assertEqual(herdr.call_args.args, ("pane", "close", "crew-pane"))

    def test_binding_is_immutable(self):
        lifetime.bind(self.session, self.ref, self.crew)
        with self.assertRaises(CaptainError):
            lifetime.bind(self.session, self.ref, {**self.crew, "pane": "other"})

    def test_repeated_cleanup_keeps_verified_closure_after_name_reuse(self):
        lifetime.bind(self.session, self.ref, self.crew)
        self.stopped()
        pane = lifetime.read(self.session, self.ref)["pane"]
        with patch.object(lifetime.runtime, "herdr", return_value={"pane": pane}):
            self.assertTrue(execution.stop(self.session, self.ref).ok)
        self.session.meta["crew"]["will"] = {**self.crew, "incarnation_id": "c" * 32}
        store.write_json(self.session.meta_path, self.session.meta)
        for _ in range(2):
            with patch.object(lifetime.runtime, "herdr", side_effect=CaptainError("gone")) as herdr:
                self.assertTrue(execution.stop(self.session, self.ref).ok)
                self.assertTrue(lifetime.close(self.session, self.ref).ok)
                herdr.assert_not_called()
        self.assertEqual(lifetime.read(self.session, self.ref)["close_state"], "closed")
        self.assertFalse(lifetime.pending(self.session.directory))

    def test_escaped_descendant_retains_worktree_but_not_native_history(self):
        lifetime.revoke(self.session, self.ref)
        identity = {
            "crew_id": "will",
            "incarnation_id": self.ref,
            "root": str(self.project),
            "worktree": None,
        }
        accepted = {("subreaper", "none-surviving"), ("descendant-scan", "none-observed")}
        for evidence, state in (
            *accepted,
            ("subreaper", "survived"),
            ("subreaper", "unverified"),
            ("descendant-scan", "observed"),
            ("descendant-scan", "unverified"),
            ("none", "unverified"),
            (None, "none-reported"),
        ):
            with self.subTest(evidence=evidence, state=state):
                self.stopped()
                path = self.session.directory / "lifetimes" / f"{self.ref}.json"
                record = store.read_json(path)
                record["shutdown"].update(escape_evidence=evidence, escape_state=state)
                store.write_json(path, record)
                self.assertEqual(self.verdict().ok, (evidence, state) in accepted)
                self.assertTrue(
                    lifetime.check(
                        self.session, self.ref, **identity, closure=False, scope="native"
                    ).ok
                )

    def test_native_history_needs_the_reaped_native_exit(self):
        lifetime.revoke(self.session, self.ref)
        identity = {
            "crew_id": "will",
            "incarnation_id": self.ref,
            "root": str(self.project),
            "worktree": None,
        }
        identity.update(closure=False, scope="native")
        self.stopped(native_returncode=None)
        self.assertFalse(lifetime.check(self.session, self.ref, **identity).ok)
        self.stopped(shutdown=None)
        self.assertFalse(lifetime.check(self.session, self.ref, **identity).ok)
        self.stopped(native_returncode=-9)
        self.assertTrue(lifetime.check(self.session, self.ref, **identity).ok)


class CwdSweepTests(SessionCase):
    """Worktree removal sees processes still working inside it, and never kills them."""

    def setUp(self):
        super().setUp()
        self.tree = self.root / "tree"
        (self.tree / "sub").mkdir(parents=True)

    def test_proc_sweep_finds_holders_under_root_only(self):
        proc = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for pid, target in (("123", self.tree / "sub"), ("124", self.project), ("125", None)):
            (proc / pid).mkdir()
            if target:
                (proc / pid / "cwd").symlink_to(target)
        (proc / "126").mkdir()
        (proc / "126" / "cwd").symlink_to(f"{self.tree} (deleted)")
        with patch.object(lifetime, "_LINUX", True), patch.object(lifetime, "_PROC", proc):
            self.assertEqual(lifetime.cwd_holders(self.tree), [123, 126])

    def lsof(self):
        return patch.multiple(lifetime, _LINUX=False)

    def test_lsof_sweep_finds_live_holder_without_signalling_it(self):
        if not Path(lifetime._LSOF).exists():
            self.skipTest("lsof unavailable")
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.tree / "sub"
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        with self.lsof():
            self.assertIn(holder.pid, lifetime.cwd_holders(self.tree))
        self.assertIsNone(holder.poll())
        with self.lsof():
            self.assertNotIn(holder.pid, lifetime.cwd_holders(self.project))

    def test_missing_or_failing_lsof_retains(self):
        for tool in (str(self.root / "missing-lsof"), "/usr/bin/false"):
            with (
                self.subTest(tool=tool),
                self.lsof(),
                patch.object(lifetime, "_LSOF", tool),
                self.assertRaisesRegex(CaptainError, "cleanup-pending"),
            ):
                lifetime.cwd_holders(self.tree)
