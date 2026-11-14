"""A crew whose owner died without a receipt dismisses only once its native group is gone."""

import json
import os
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import execution, lifetime, sessions, store
from captain_barbossa.runtime import CaptainError


class LostOwnerDismissTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\n"
            "poll_interval=0.01\nterminate_timeout=0.1\nshutdown_timeout=2.0\n",
        )
        self.session = sessions.session(
            self.project,
            {"workspace_id": "workspace", "pane_id": "captain"},
            session_id="a" * 32,
            create=True,
        )
        # Unique, because the legacy sweep matches it in every live process.
        incarnation = uuid.uuid4().hex
        self.crew = {
            "id": "will",
            "incarnation_id": incarnation,
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
        self.path = self.session.directory / "lifetimes" / f"{incarnation}.json"

    def prepare(self, argv):
        ref = lifetime.prepare(self.session, self.crew, argv, {}, self.project)
        lifetime.bind(self.session, ref, self.crew)
        return ref

    def verdict(self, ref, scope):
        return lifetime.check(
            self.session,
            ref,
            crew_id="will",
            incarnation_id=ref,
            root=str(self.project),
            worktree=None,
            closure=False,
            scope=scope,
        )

    def record(self, **fields):
        record = store.read_json(self.path)
        record.update(fields)
        store.write_json(self.path, record)

    def test_killed_owner_and_vanished_pane_dismiss_with_group_proof_only(self):
        ref = self.prepare([sys.executable, "-c", "import time; time.sleep(30)"])
        code = (
            "import os,sys; from pathlib import Path; "
            "from captain_barbossa import lifetime,sessions,store; "
            "p=Path(sys.argv[1]); s=sessions.Session(p,store.read_json(p/'session.json')); "
            "c=lifetime.claim(s,sys.argv[2]); job=c.__enter__(); "
            "job.start(detached=True); os._exit(0)"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(self.session.directory), ref],
            check=True,
            timeout=10,
            capture_output=True,
            cwd=self.project,
        )
        group = lifetime.read(self.session, ref)["group"]
        self.assertIs(type(group), int)
        # The guard kills its group once the owner's lifeline closes.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lifetime._reap_group_zombies(group)
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("guard group outlived its owner")
        self.assertEqual(lifetime.read(self.session, ref)["state"], "claimed")
        with patch.object(
            lifetime.runtime, "herdr", side_effect=CaptainError("pane_not_found")
        ) as herdr:
            verdict = execution.stop(self.session, ref)
        self.assertEqual(verdict, lifetime.Verdict(True, "owner-lost-group-absent"))
        self.assertEqual(herdr.call_args.args, ("pane", "get", "crew-pane"))
        self.assertEqual(lifetime.read(self.session, ref)["close_state"], "closed")
        # No receipt saw the native exit or excluded escapes.
        self.assertEqual(self.verdict(ref, "native").reason, "native-exit-unreported")
        self.assertEqual(self.verdict(ref, "worktree").reason, "descendant-escape-unexcluded")
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_live_owner_surviving_group_guard_error_or_unrecorded_group_retain(self):
        ref = self.prepare(["native"])
        with lifetime.claim(self.session, ref):
            self.record(group=987654)
            lifetime.revoke(self.session, ref)
            self.assertEqual(self.verdict(ref, "group").reason, "missing-invalid-or-owned")
        for alive in (None, PermissionError("recycled")):
            with (
                self.subTest(alive=alive),
                patch.object(lifetime.os, "killpg", side_effect=[alive]),
            ):
                self.assertEqual(self.verdict(ref, "group").reason, "managed-group-alive")
        absent = patch.object(lifetime.os, "killpg", side_effect=ProcessLookupError())
        with absent:
            self.assertTrue(self.verdict(ref, "group").ok)
        self.record(guard_error=True)
        with absent:
            self.assertEqual(self.verdict(ref, "group").reason, "guard-reported-error")
        self.record(guard_error=False, group=None)
        with absent:
            self.assertEqual(self.verdict(ref, "group").reason, "exit-unknown")

    def test_legacy_claim_without_group_releases_shared_scope_only_with_no_carrier(self):
        ref = self.prepare(["native"])
        with lifetime.claim(self.session, ref):
            pass
        lifetime.revoke(self.session, ref)
        self.assertIsNone(lifetime.read(self.session, ref)["group"])
        record = store.read_json(self.path)
        del record["group"]
        store.write_json(self.path, record)
        self.assertEqual(self.verdict(ref, "worktree").reason, "exit-unknown")
        self.assertEqual(self.verdict(ref, "native").reason, "exit-unknown")
        carrier = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={**os.environ, "CAPTAIN_INCARNATION": ref},
        )
        try:
            self.assertEqual(self.verdict(ref, "group").reason, "incarnation-process-alive")
        finally:
            carrier.kill()
            carrier.wait(timeout=5)
        failed = subprocess.CompletedProcess([], 1, b"", b"")
        with (
            patch.object(lifetime.subprocess, "run", return_value=failed),
            patch.object(lifetime, "_pids", side_effect=PermissionError("hidden")),
        ):
            self.assertEqual(self.verdict(ref, "group").reason, "exit-unknown")
        with patch.object(lifetime.runtime, "herdr", side_effect=CaptainError("pane_not_found")):
            verdict = execution.stop(self.session, ref)
        self.assertEqual(verdict, lifetime.Verdict(True, "owner-lost-no-carrier"))

    def test_guard_error_is_recorded_before_its_owner_can_be_lost(self):
        ref = self.prepare(["native"])
        with lifetime.claim(self.session, ref) as job:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, json.dumps({"type": "error"}).encode() + b"\n")
            os.close(write_fd)
            os.set_blocking(read_fd, False)
            job._status = read_fd
            with self.assertRaises(CaptainError):
                job.poll()
        self.assertIs(lifetime.read(self.session, ref)["guard_error"], True)
