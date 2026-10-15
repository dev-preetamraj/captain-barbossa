"""Native lifetime uses inherited terminal descriptors and direct-child waits."""

import json
import os
import select
import signal
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from home_isolation import SessionCase

from captain_barbossa import lifetime, sessions, store
from captain_barbossa.runtime import CaptainError


class InteractiveLifetimeTests(SessionCase):
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
        self.crew = {"id": "will", "incarnation_id": "b" * 32}
        self.session.meta["crew"]["will"] = self.crew
        store.write_json(self.session.meta_path, self.session.meta)

    def prepare(self, argv=None, env=None):
        return lifetime.prepare(
            self.session, self.crew, argv or ["native"], env or {}, self.project
        )

    def test_inherited_stdio_session_group_environment_and_signal_handlers(self):
        ref = self.prepare(env={"CAPTAIN_CREW": "will"})
        before = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }

        def started(job):
            job.native_returncode = -signal.SIGINT

        with (
            patch.dict(os.environ, {"NATIVE_SECRET": "inherited-only"}),
            patch.object(lifetime.ManagedJob, "start", autospec=True, side_effect=started) as start,
            patch.object(lifetime.ManagedJob, "poll", return_value=-signal.SIGINT),
            patch.object(
                lifetime.ManagedJob, "shutdown", return_value=lifetime.Verdict(True, "stopped")
            ),
            patch.object(
                lifetime.ManagedJob, "finish", return_value=lifetime.Verdict(True, "stopped")
            ),
        ):
            self.assertEqual(lifetime.run(self.session.directory, ref), -signal.SIGINT)
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs, {})
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})
        self.assertNotIn("NATIVE_SECRET", lifetime.read(self.session, ref)["env"])

    def test_actual_child_uses_existing_terminal_and_owned_foreground_group(self):
        output = self.project / "observed"
        script = (
            "import os,pathlib,json; text=input(); pathlib.Path('observed').write_text("
            "json.dumps([os.getsid(0),os.getpgrp(),os.tcgetpgrp(0),"
            "os.isatty(0),os.isatty(1),os.isatty(2),text,os.environ['CAPTAIN_CREW']]))"
        )
        ref = self.prepare([sys.executable, "-c", script], {"CAPTAIN_CREW": "will"})
        wrapper = (
            "import fcntl,json,os,sys,termios; from pathlib import Path; "
            "from captain_barbossa import lifetime; "
            "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
            "before=termios.tcgetattr(0); result=lifetime.run(sys.argv[1],sys.argv[2]); "
            "assert os.tcgetpgrp(0)==os.getpgrp(); assert termios.tcgetattr(0)==before; "
            "Path('owner').write_text(json.dumps([os.getsid(0),os.getpgrp(),result]))"
        )
        master, slave = os.openpty()
        try:
            owner = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(self.session.directory), ref],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                cwd=self.project,
            )
            try:
                os.write(master, b"hello\n")
                # Drain the echo: an owner exiting with unread terminal output can block.
                deadline = time.monotonic() + 15
                while owner.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        os.read(master, 4096)
                self.assertEqual(owner.wait(timeout=1), 0)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
        finally:
            os.close(master)
            os.close(slave)
        native = json.loads(output.read_text())
        owner_state = json.loads((self.project / "owner").read_text())
        self.assertEqual(native[0], owner_state[0])
        self.assertNotEqual(native[1], owner_state[1])
        self.assertEqual(native[1], native[2])
        self.assertEqual(native[3:], [True, True, True, "hello", "will"])
        self.assertEqual(lifetime.read(self.session, ref)["state"], "exited")

    def test_failed_spawn_retains_unknown(self):
        ref = self.prepare()
        with patch.object(lifetime.subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(CaptainError), lifetime.claim(self.session, ref) as job:
                job.start(detached=True)
        self.assertEqual(lifetime.read(self.session, ref)["state"], "claimed")
        lifetime.revoke(self.session, ref)
        self.assertTrue(lifetime.pending(self.session.directory))
        with patch.object(lifetime.subprocess, "Popen") as popen:
            with self.assertRaises(CaptainError):
                lifetime.run(self.session.directory, ref)
            popen.assert_not_called()

    def test_busy_owner_cannot_be_claimed_twice(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref):
            with self.assertRaises(CaptainError), lifetime.claim(self.session, ref):
                self.fail("second owner")

    def test_stale_incarnation_cannot_launch(self):
        ref = self.prepare()
        self.session.meta["crew"]["will"]["incarnation_id"] = "c" * 32
        store.write_json(self.session.meta_path, self.session.meta)
        with patch.object(lifetime.subprocess, "Popen") as popen:
            with self.assertRaises(CaptainError):
                lifetime.run(self.session.directory, ref)
            popen.assert_not_called()

    def test_lost_wait_does_not_record_exit(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            job._guard = Mock(pid=987654)
            job._guard.wait.side_effect = subprocess.TimeoutExpired("guard", 1)
            with (
                patch.object(lifetime.os, "killpg"),
                patch.object(job, "_drain"),
                patch.object(lifetime.time, "monotonic", side_effect=[0, 100, 100, 100]),
            ):
                self.assertFalse(job.shutdown().ok)
                self.assertFalse(job.finish().ok)
        lifetime.revoke(self.session, ref)
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_real_owner_crash_never_becomes_exit_receipt(self):
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
        lifetime.revoke(self.session, ref)
        self.assertTrue(lifetime.pending(self.session.directory))

    def test_secrets_cannot_be_persisted_as_launch_overrides(self):
        for env in ({"API_KEY": "secret"}, {"CAPTAIN_AUTH_TOKEN": "secret"}, dict(os.environ)):
            with self.subTest(keys=list(env)[:2]), self.assertRaises(CaptainError):
                self.prepare(env=env)

    def test_surviving_term_ignoring_writer_needs_final_group_kill_before_receipt(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            job._guard = Mock(pid=987654)
            job._guard.wait.return_value = -signal.SIGKILL
            job.native_returncode = 0
            events = []

            def group_signal(group, signum):
                events.append((group, signum))
                if signum == 0:
                    raise ProcessLookupError()

            def reaped(**_):
                self.assertEqual(events[-1], (987654, signal.SIGKILL))
                events.append("reaped")
                return -signal.SIGKILL

            job._guard.wait.side_effect = reaped
            self.assertFalse(job.finish().ok)
            with (
                patch.object(lifetime.os, "killpg", side_effect=group_signal),
                patch.object(job, "_drain"),
                patch.object(lifetime.time, "monotonic", side_effect=[0, 1, 100, 100, 100]),
                patch.object(lifetime.time, "sleep"),
            ):
                self.assertTrue(job.shutdown().ok)
            self.assertEqual(
                events,
                [
                    (987654, signal.SIGTERM),
                    (987654, signal.SIGCONT),
                    (987654, signal.SIGKILL),
                    "reaped",
                    (987654, 0),
                ],
            )
            self.assertTrue(job.finish().ok)
        record = lifetime.read(self.session, ref)
        self.assertEqual(record["receipt_version"], 2)
        self.assertTrue(record["shutdown"]["group_absent"])

    def test_ordinary_descendant_outlives_native_until_managed_shutdown(self):
        writer = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
        native = (
            "import subprocess,sys; "
            f"writer=subprocess.Popen([sys.executable,'-c',{writer!r}],stdout=subprocess.PIPE); "
            "assert writer.stdout.readline()==b'ready\\n'; print('survivor-ready',flush=True)"
        )
        ref = self.prepare([sys.executable, "-c", native])
        with lifetime.claim(self.session, ref) as job:
            job.start(detached=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(job.stdout.close)
            self.addCleanup(job.stderr.close)
            self.assertTrue(select.select([job.stdout], [], [], 5)[0])
            self.assertEqual(job.stdout.readline(), b"survivor-ready\n")
            for _ in range(3):
                if job.poll() is not None:
                    break
                self.assertTrue(select.select([job._status], [], [], 5)[0])
            self.assertEqual(job.native_returncode, 0)
            self.assertFalse(job.finish().ok)
            with patch.object(
                lifetime,
                "_duration",
                side_effect=lambda name: {
                    "terminate_timeout": 0.1,
                    "shutdown_timeout": 2.0,
                    "poll_interval": 0.01,
                }[name],
            ):
                stopped = job.shutdown()
            if stopped.ok:
                self.assertTrue(job.finish().ok)
                if sys.platform == "darwin":
                    # An in-group descendant is not an escape: worktree removal stays allowed.
                    shutdown = lifetime.read(self.session, ref)["shutdown"]
                    self.assertEqual(shutdown["escape_state"], "none-observed")
            else:
                # A host that has not reaped orphan zombies cannot prove group absence.
                self.assertEqual(stopped.reason, "managed-group-absence-unconfirmed")
                self.assertFalse(job.finish().ok)

    def test_reused_group_or_permission_error_retains_and_never_signals_after_reap(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            job._guard = Mock(pid=987654)
            job._guard.wait.return_value = -signal.SIGKILL
            calls = []

            def occupied(group, signum):
                calls.append(signum)
                if job._reaped:
                    self.assertEqual(signum, 0)
                    raise PermissionError("replacement group")

            with (
                patch.object(lifetime.os, "killpg", side_effect=occupied),
                patch.object(job, "_drain"),
                patch.object(lifetime.time, "monotonic", side_effect=[0, 100, 100, 100]),
            ):
                self.assertFalse(job.shutdown().ok)
            self.assertFalse(job.finish().ok)
            self.assertEqual(calls[-1], 0)

    def test_guard_control_loss_surfaces_instead_of_looking_active(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            read_fd, write_fd = os.pipe()
            job._status = read_fd
            os.close(write_fd)
            with self.assertRaises(CaptainError):
                job.poll()
            self.assertFalse(job.finish().ok)

    def test_known_escaped_pipe_holder_blocks_finish(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            job._reaped = True
            job._shutdown = lifetime.Verdict(True, "group-absent")
            job.invalidate("escaped-pipe-holder")
            self.assertFalse(job.finish().ok)

    def test_terminal_restore_does_not_steal_replacement_foreground(self):
        ref = self.prepare()
        with lifetime.claim(self.session, ref) as job:
            job._terminal = (123, ["saved-attributes"])
            job._guard = Mock(pid=456)
            job._shutdown = lifetime.Verdict(False, "fixture")
            with (
                patch.object(lifetime.termios, "tcsetattr") as restore,
                patch.object(lifetime.os, "tcgetpgrp", return_value=789),
                patch.object(lifetime.os, "tcsetpgrp") as foreground,
            ):
                job._restore_terminal()
            restore.assert_called_once_with(0, lifetime.termios.TCSANOW, ["saved-attributes"])
            foreground.assert_not_called()

    def escaping_native(self):
        marker = self.project / "escapee"
        escapee = (
            "import os,sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
        )
        native = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{escapee!r},{str(marker)!r}],"
            "start_new_session=True); "
            f"[time.sleep(0.01) for _ in range(500) if not Path({str(marker)!r}).exists()]; "
            "time.sleep(0.5)"
        )

        def kill():
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

        self.addCleanup(kill)
        return self.prepare([sys.executable, "-c", native])

    def stop_escaped(self, ref):
        with lifetime.claim(self.session, ref) as job:
            job.start(detached=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(1000):
                if job.poll() is not None:
                    break
                select.select([job._status], [], [], 0.05)
            self.assertEqual(job.native_returncode, 0)
            self.assertTrue(job.shutdown().ok)
            self.assertTrue(job.finish().ok)
        lifetime.revoke(self.session, ref)
        identity = {
            "crew_id": "will",
            "incarnation_id": ref,
            "root": str(self.project),
            "worktree": None,
        }
        receipt = lifetime.read(self.session, ref)["shutdown"]
        self.assertFalse(lifetime.check(self.session, ref, **identity).ok)
        self.assertTrue(
            lifetime.check(self.session, ref, **identity, closure=False, scope="native").ok
        )
        return receipt

    @unittest.skipUnless(sys.platform == "darwin", "Darwin descendant scan")
    def test_darwin_scan_sees_setsid_escape_and_retains_worktree_only(self):
        receipt = self.stop_escaped(self.escaping_native())
        self.assertEqual(
            (receipt["escape_evidence"], receipt["escape_state"]), ("descendant-scan", "observed")
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper")
    def test_linux_subreaper_finds_surviving_setsid_escape(self):
        receipt = self.stop_escaped(self.escaping_native())
        self.assertEqual(
            (receipt["escape_evidence"], receipt["escape_state"]), ("subreaper", "survived")
        )

    def test_linux_receipt_reports_subreaped_survivors_or_unverified(self):
        for baseline, after, expected in (
            ({1}, {1}, "none-surviving"),
            ({1}, {1, 4242}, "survived"),
            (None, {1}, "unverified"),
        ):
            with self.subTest(expected=expected):
                self.crew["incarnation_id"] = uuid.uuid4().hex
                store.write_json(self.session.meta_path, self.session.meta)
                ref = self.prepare()
                with (
                    patch.object(lifetime, "_LINUX", True),
                    patch.object(lifetime, "_DARWIN", False),
                    patch.object(lifetime, "_subreaper"),
                    patch.object(lifetime, "_live_children", return_value=after),
                    lifetime.claim(self.session, ref) as job,
                ):
                    job._guard = Mock(pid=987654)
                    job._guard.wait.return_value = -signal.SIGKILL
                    job._baseline = baseline
                    job.native_returncode = 0

                    def group_signal(_group, signum):
                        if signum == 0:
                            raise ProcessLookupError()

                    with (
                        patch.object(lifetime.os, "killpg", side_effect=group_signal),
                        patch.object(job, "_drain"),
                        patch.object(lifetime.time, "monotonic", side_effect=[0, 100, 100, 100]),
                    ):
                        self.assertTrue(job.shutdown().ok)
                    self.assertTrue(job.finish().ok)
                shutdown = lifetime.read(self.session, ref)["shutdown"]
                self.assertEqual(shutdown["escape_evidence"], "subreaper")
                self.assertEqual(shutdown["escape_state"], expected)
