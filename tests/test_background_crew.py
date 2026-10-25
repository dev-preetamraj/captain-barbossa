"""Background crew: a paneless read-only turn on the existing assignment and wait lifecycle.

The native CLI is a stub script and the owner is the real detached process, so these run
without a model or Herdr.
"""

import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from captain_barbossa import agents, background, cli, lifetime, protocol, runtime, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import HERDR, SessionCase

# A codex `exec --json` turn that finished with an answer.
REPORT = (
    {"type": "item.completed", "item": {"type": "agent_message", "text": "slow path is X"}},
    {"type": "turn.completed", "usage": {}},
)
SAY = "printf '%s\\n' " + " ".join(f"'{json.dumps(event)}'" for event in REPORT)


class BackgroundCrewTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.herdr = self.enterContext(patch.object(runtime, "herdr"))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def native(self, body):
        path = self.bin / "codex"
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        self.enterContext(patch.object(agents, "executable", return_value=str(path)))

    def args(self, *extra):
        return cli.parser().parse_args(["--session", self.meta["id"], *extra])

    def recruit(self, *extra):
        args = self.args(
            "crew", "--background", "--agent", "codex", "--task", "find the slow path", *extra
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            agents.create_crew(args, self.pane, self.project)
        return json.loads(out.getvalue().splitlines()[-1])

    def crew(self):
        return Crew.resolve(sessions.read_session(self.project, self.meta["id"]), "jack")

    def finished(self, crew):
        path = background.paths(background.stem(crew))["record"]
        deadline = time.monotonic() + 20
        while store.read_json(path)["state"] != "finished":
            if time.monotonic() > deadline:
                self.fail(f"owner never finished: {store.read_json(path)}")
            time.sleep(0.05)
        return store.read_json(path)

    def assignment(self, crew):
        return protocol.active_assignment(crew)

    def pids(self, path, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = [int(pid) for pid in path.read_text().split()] if path.exists() else []
            if len(found) >= count:
                return found
            time.sleep(0.05)
        self.fail(f"the stub never recorded {count} pids")

    def assertGone(self, pids):
        deadline = time.monotonic() + 10
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    os.kill(pid, signal.SIGKILL)
                    self.fail(f"process {pid} outlived its run")
                time.sleep(0.05)

    def test_recruit_touches_no_pane_and_its_report_arrives_through_wait(self):
        self.native(f"{SAY}")
        record = self.recruit()
        self.herdr.assert_not_called()
        self.assertEqual(record["execution"], "background")
        self.assertNotIn("pane", record)
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "done")
        response = protocol.wait(crew, 0)
        self.assertEqual((response["status"], response["summary"]), ("done", "slow path is X"))
        self.assertEqual(protocol.wait(crew, 0, ack=response["delivery_id"])["status"], "idle")
        # The headless argv is quiet's read-only gate, with no access to session state.
        launch = store.read_json(background.paths(background.stem(crew))["record"])
        self.assertNotIn("argv", launch)
        self.assertNotIn("env", launch)
        argv = lifetime.read(crew.session, launch["lifetime_id"])["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", argv)
        self.assertNotIn("--add-dir", argv)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            agents.status_crew(self.args("status"), None, self.project)
        self.assertRegex(out.getvalue(), r"Jack .*background +state:done source:mail")
        with contextlib.redirect_stdout(io.StringIO()):
            agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        self.herdr.assert_not_called()

    def test_poll_before_prepare_writes_the_record_keeps_the_live_recruiter(self):
        self.native(SAY)
        prepare = background.prepare

        def poll_before_record(crew, *args):
            self.assertFalse(background.paths(background.stem(crew))["record"].exists())
            self.assertTrue(background.alive(crew))
            response = protocol.wait(crew, 0)
            self.assertIsNone(response["delivery_id"])
            self.assertEqual(self.assignment(crew)["state"], "working")
            return prepare(crew, *args)

        with patch.object(background, "prepare", side_effect=poll_before_record) as preparing:
            self.recruit()
        preparing.assert_called_once()
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "done")
        self.assertEqual(protocol.wait(crew, 0)["summary"], "slow path is X")
        self.herdr.assert_not_called()

    def test_poll_immediately_after_begin_keeps_the_live_recruiter(self):
        self.native(SAY)
        begin = protocol.begin

        def poll_after_begin(crew, *args, **kwargs):
            assignment = begin(crew, *args, **kwargs)
            self.assertFalse(background.paths(background.stem(crew))["record"].exists())
            response = protocol.wait(crew, 0)
            self.assertIsNone(response["delivery_id"])
            self.assertEqual(self.assignment(crew)["state"], "working")
            return assignment

        with patch.object(protocol, "begin", side_effect=poll_after_begin) as beginning:
            self.recruit()
        beginning.assert_called_once()
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "done")
        self.assertEqual(protocol.wait(crew, 0)["summary"], "slow path is X")
        self.herdr.assert_not_called()

    def test_mixed_or_writing_flags_reserve_nothing(self):
        self.native("exit 0")
        for extra in (
            ("--placement", "tab"),
            ("--split-pane", "w1:p1"),
            ("--owns", "src"),
            ("--allow", "edit"),
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(CaptainError, "background"):
                self.recruit(*extra)
        self.assertFalse((self.directory / "protocol.json").exists())
        self.assertEqual(store.read_json(self.directory / "session.json")["crew"], {})

    def test_a_zero_exit_without_a_valid_result_is_a_failure_not_done(self):
        self.native("echo 'not json'; echo oops >&2")
        self.recruit()
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "failed")
        response = protocol.wait(crew, 0)
        self.assertEqual(response["status"], "error")
        self.assertIn("[background-failed] exit 0", response["summary"])
        self.assertIn("oops", response["summary"])
        self.assertEqual(crew.current_state().state, "failed")

    def test_interrupt_stops_the_run_through_its_owner_and_keeps_its_files(self):
        marker = self.project / "partial.txt"
        pid_file = self.root / "native.pid"
        self.native(f"echo $$ > {pid_file}; echo partial > {marker}; exec sleep 30")
        self.recruit()
        crew = self.crew()
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            agents.interrupt_crew(
                self.args("interrupt", "jack", "--reason", "stop"), None, self.project
            )
        self.assertIn("Jack: cancelled.", out.getvalue())
        self.assertEqual(self.finished(crew)["outcome"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        self.assertTrue(marker.exists())
        response = protocol.wait(crew, 0)
        self.assertEqual(response["status"], "error")
        self.assertIn("[background-cancelled] stop", response["summary"])

    def test_interrupt_records_memory_only_when_background_cancellation_takes_effect(self):
        self.native(f"{SAY}")
        self.recruit()
        crew = self.crew()
        self.finished(crew)
        outcomes = (
            ("cancelled", True),
            ("not running; nothing was signalled", False),
            ("already finished as done; the cancel did not take effect", False),
            ("cancel requested; the owner has not confirmed it yet", False),
        )
        for outcome, recorded in outcomes:
            with (
                self.subTest(outcome=outcome),
                patch.object(background, "cancel", return_value=outcome),
                patch.object(agents, "add_memory") as add_memory,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                agents.interrupt_crew(
                    self.args("interrupt", "jack", "--reason", "late"), None, self.project
                )
            self.assertEqual(out.getvalue(), f"Jack: {outcome}.\n")
            if recorded:
                add_memory.assert_called_once_with(
                    crew.session.graph, crew.record["agent"], "interrupted", "late"
                )
            else:
                add_memory.assert_not_called()

    def test_the_configured_timeout_stops_the_run_as_a_failure(self):
        self.native("exec sleep 30")
        with patch.object(background.config, "number", return_value=0.5):
            self.recruit()
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "failed")
        self.assertIn("[background-timeout]", protocol.wait(crew, 0)["summary"])

    def test_a_lost_owner_takes_its_native_work_down_and_is_never_restarted(self):
        pids = self.root / "native.pids"
        # The native CLI and a descendant it spawned: neither may outlive the owner.
        self.native(f"sleep 30 & echo $! >> {pids}; echo $$ >> {pids}; exec sleep 30")
        self.recruit()
        crew = self.crew()
        files = background.paths(background.stem(crew))
        self.assertTrue(background.alive(crew))
        running = self.pids(pids, 2)
        os.kill(store.read_json(files["record"])["owner"], signal.SIGKILL)
        self.assertGone(running)
        self.assertFalse(background.alive(crew))
        # Status reads the loss without recording it; the next wait records and delivers it.
        self.assertEqual(crew.current_state().state, "failed")
        self.assertIn("[background-lost]", crew.current_state().detail)
        response = protocol.wait(crew, 0)
        self.assertEqual(response["status"], "error")
        self.assertIn("[background-lost]", response["summary"])
        self.assertEqual(self.assignment(crew)["outcome"], "lost")
        self.assertEqual(store.read_json(files["record"])["state"], "running")

    def test_a_descendant_holding_the_output_pipes_cannot_hang_the_owner(self):
        pids = self.root / "native.pids"
        self.native(f"sleep 30 & echo $! >> {pids}; {SAY}")
        self.recruit()
        crew = self.crew()
        started = time.monotonic()
        self.assertEqual(self.finished(crew)["outcome"], "done")
        self.assertLess(time.monotonic() - started, 10)
        self.assertGone(self.pids(pids, 1))
        self.assertEqual(protocol.wait(crew, 0)["summary"], "slow path is X")

    def test_cancel_kills_descendants_that_ignore_term_after_the_cli_exits(self):
        pids = self.root / "native.pids"
        self.native(
            f"(trap '' TERM; exec sleep 30) & echo $! >> {pids}; echo $$ >> {pids}; exec sleep 30"
        )
        self.recruit()
        crew = self.crew()
        running = self.pids(pids, 2)
        self.assertEqual(background.cancel(crew, "stop"), "cancelled")
        self.assertGone(running)
        self.assertEqual(self.assignment(crew)["outcome"], "cancelled")

    def test_cancel_never_claims_a_run_that_completion_won(self):
        self.native(f"{SAY}")
        self.recruit()
        crew = self.crew()
        self.finished(crew)
        with patch.object(background, "alive", return_value=True):
            outcome = background.cancel(crew, "late")
        self.assertEqual(outcome, "already finished as done; the cancel did not take effect")
        self.assertEqual(protocol.wait(crew, 0)["status"], "done")

    def test_startup_gives_up_on_an_owner_that_never_claims_and_a_late_claim_runs_nothing(self):
        marker = self.root / "ran"
        self.native(f"touch {marker}; {SAY}")
        stalled = self.bin / "python"
        stalled.write_text("#!/bin/sh\nexec sleep 3\n", encoding="utf-8")
        stalled.chmod(0o755)
        started = time.monotonic()
        with (
            patch.object(background, "STARTUP", 0.5),
            patch.object(sys, "executable", str(stalled)),
            self.assertRaisesRegex(CaptainError, r"\[background-start\]"),
        ):
            self.recruit()
        self.assertLess(time.monotonic() - started, 5)
        crew = self.crew()
        files = background.paths(background.stem(crew))
        self.assertTrue(crew.is_dismissed)
        self.assertEqual(store.read_json(files["record"])["state"], "abandoned")
        self.assertEqual(self.assignment(crew)["outcome"], "failed")
        notices = len(self.assignment(crew)["notices"])
        background.run(files["record"])  # the owner arriving after the deadline
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.assignment(crew)["notices"]), notices)

    def test_an_owner_stopped_inside_the_protocol_lock_cannot_hold_startup_open(self):
        marker = self.root / "ran"
        self.native(f"touch {marker}; {SAY}")
        # Stands in for an owner stopped mid-claim: it holds the protocol lock and stalls.
        holder = self.bin / "python"
        holder.write_text(
            f"#!{sys.executable}\nimport fcntl, json, os, sys, time\n"
            "record = json.load(open(sys.argv[3]))\n"
            "lock = open(os.path.join(record['session'], 'protocol.lock'), 'a')\n"
            "fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "open(sys.argv[3] + '.pid', 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        holder.chmod(0o755)
        started = time.monotonic()
        with (
            patch.object(background, "STARTUP", 1),
            patch.object(background, "LOCK_WAIT", 0.3),
            patch.object(sys, "executable", str(holder)),
            self.assertRaisesRegex(CaptainError, r"\[background-start\]"),
        ):
            self.recruit()
        self.assertLess(time.monotonic() - started, 5)
        crew = self.crew()
        files = background.paths(background.stem(crew))
        holding = self.pids(files["record"].with_name(files["record"].name + ".pid"), 1)
        self.assertTrue(crew.is_dismissed)
        self.assertEqual(store.read_json(files["record"])["state"], "abandoned")
        os.kill(holding[0], signal.SIGKILL)  # its lock goes with it, even unreaped
        background.run(files["record"])  # the stopped owner resuming after the deadline
        self.assertFalse(marker.exists())
        # The bookkeeping the held lock prevented is done by the next wait.
        response = protocol.wait(crew, 0)
        self.assertEqual(response["status"], "error")
        self.assertIn("[background-start]", response["summary"])

    def test_a_recruiter_dying_at_any_step_leaves_a_visible_crew(self):
        self.native("exit 0")
        for step, name in ((protocol, "begin"), (background, "prepare"), (background, "start")):
            with (
                self.subTest(step=name),
                patch.object(step, name, side_effect=SystemExit("recruiter killed")),
                self.assertRaises(SystemExit),
            ):
                self.recruit()
            current = sessions.read_session(self.project, self.meta["id"])
            crew = Crew.resolve(current, "jack")
            self.assertEqual(crew.record["status"], "starting")
            self.assertTrue(Crew.name_reserved(current, "jack"))
            self.assertEqual(crew.current_state().state, "failed")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                agents.status_crew(self.args("status"), None, self.project)
            self.assertIn("state:failed source:process", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                agents.wait_crew(
                    self.args("wait", "--all", "--json", "--timeout", "0"), None, self.project
                )
            deliveries = json.loads(out.getvalue())["deliveries"]
            self.assertEqual(len(deliveries), 1)
            response = deliveries[0]
            self.assertEqual(response["status"], "error")
            if name != "begin":
                self.assertTrue(sessions.unresolved(self.directory))
                self.assertIsNotNone(response["delivery_id"])
                self.assertIn("[background-lost]", response["summary"])
                self.assertEqual(self.assignment(crew)["outcome"], "lost")
                protocol.wait(crew, 0, ack=response["delivery_id"])
            else:
                self.assertIsNone(response["delivery_id"])
            with contextlib.redirect_stdout(io.StringIO()):
                agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)

    def test_status_and_fleet_wait_report_unreserved_background_recruits(self):
        self.native("exit 0")
        # Establish real protocol state for a former incarnation of this name.
        with (
            patch.object(background, "prepare", side_effect=SystemExit("recruiter killed")),
            self.assertRaises(SystemExit),
        ):
            self.recruit()
        with contextlib.redirect_stdout(io.StringIO()):
            agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        previous = self.assignment(self.crew())["id"]
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(
            patch.object(cli, "current_pane", side_effect=AssertionError("pane resolved"))
        )
        for stale in (False, True):
            with self.subTest(stale=stale):
                with protocol.checkpoint(self.directory) as state:
                    if stale:
                        state["active"]["jack"] = previous
                    else:
                        state["active"].pop("jack")
                with (
                    patch.object(protocol, "begin", side_effect=SystemExit("recruiter killed")),
                    self.assertRaises(SystemExit),
                ):
                    self.recruit()
                before = store.read_json(self.directory / "protocol.json")
                session = ["--session", self.meta["id"]]
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main([*session, "status"]), 0)
                self.assertRegex(out.getvalue(), r"Jack .*background +state:failed source:process")
                self.assertIn("[background-start]", out.getvalue())
                self.assertIn("find the slow path", out.getvalue())
                for json_output in (True, False):
                    command = [*session, "wait", "--all", "--timeout", "0"]
                    if json_output:
                        command.append("--json")
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(cli.main(command), 0)
                    if json_output:
                        response = json.loads(out.getvalue())
                        self.assertEqual(response["status"], "ready")
                        self.assertEqual(len(response["deliveries"]), 1)
                        delivery = response["deliveries"][0]
                        self.assertEqual(delivery["crew"], "Jack")
                        self.assertEqual(delivery["status"], "error")
                        self.assertIsNone(delivery["assignment_id"])
                        self.assertIsNone(delivery["delivery_id"])
                        self.assertIn("[background-start]", delivery["summary"])
                    else:
                        self.assertIn("Jack error: [background-start]", out.getvalue())
                        self.assertIn("nothing pending to acknowledge", out.getvalue())
                        self.assertNotIn("--ack", out.getvalue())
                # Neither reading nor waiting adopts or closes the old incarnation.
                self.assertEqual(store.read_json(self.directory / "protocol.json"), before)
                with contextlib.redirect_stdout(io.StringIO()):
                    agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        self.herdr.assert_not_called()

    def test_providers_without_a_holdable_gate_and_unbounded_timeouts_reserve_nothing(self):
        self.native("exit 0")
        cases = (
            ((), patch.object(background.config, "number", return_value=float("inf")), "finite"),
            (("--agent", "grok"), contextlib.nullcontext(), r"\[background-provider\]"),
            (("--agent", "claude"), contextlib.nullcontext(), r"\[background-provider\]"),
        )
        for extra, context, error in cases:
            args = self.args("crew", "--background", "--task", "find it", *extra)
            with self.subTest(error=error), context, self.assertRaisesRegex(CaptainError, error):
                agents.create_crew(args, self.pane, self.project)
        self.assertFalse((self.directory / "protocol.json").exists())
        self.assertEqual(store.read_json(self.directory / "session.json")["crew"], {})

    def test_explicit_session_wait_and_interrupt_need_no_live_pane(self):
        self.native("exec sleep 30")
        self.recruit()
        self.enterContext(contextlib.chdir(self.project))
        # A stale Herdr environment: any pane lookup or Herdr call fails the test.
        self.herdr.side_effect = AssertionError("Herdr was called")
        self.enterContext(
            patch.object(cli, "current_pane", side_effect=AssertionError("pane resolved"))
        )
        session = ["--session", self.meta["id"]]
        for command in (
            ["wait", "jack", "--json", "--timeout", "0"],
            ["wait", "--all", "--json", "--timeout", "0"],
            ["interrupt", "jack", "--reason", "stop"],
            ["wait", "jack", "--json", "--timeout", "0"],
        ):
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main([*session, *command]), 0)
        self.assertIn("[background-cancelled] stop", json.loads(out.getvalue())["summary"])

    def test_a_fleet_wait_with_a_visual_crew_still_needs_herdr(self):
        self.native(f"{SAY}")
        self.recruit()
        with sessions.crew_meta(self.directory) as meta:
            meta["crew"]["will"] = {"id": "will", "agent": "c-will", "pane": "w1:p9"}
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(
            patch.object(
                cli,
                "current_pane",
                side_effect=CaptainError("Run captain inside a Herdr workspace."),
            )
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["--session", self.meta["id"], "wait", "--all", "--timeout", "0"])
        self.assertEqual(code, 1)
        self.assertIn("Herdr workspace", err.getvalue())
        self.herdr.assert_not_called()

    def test_a_second_owner_never_reruns_a_claimed_assignment(self):
        self.native(f"{SAY}")
        self.recruit()
        crew = self.crew()
        self.finished(crew)
        notices = len(self.assignment(crew)["notices"])
        background.run(background.paths(background.stem(crew))["record"])
        self.assertEqual(len(self.assignment(crew)["notices"]), notices)

    def test_cancel_after_lifetime_claim_before_start_frees_the_name(self):
        self.native(SAY)
        claim = lifetime.claim

        @contextlib.contextmanager
        def cancel_after_claim(session, ref):
            with claim(session, ref) as job:
                crew = self.crew()
                store.write_text(background.paths(background.stem(crew))["cancel"], "stop")
                yield job

        with (
            patch.object(
                background, "start", side_effect=lambda files: background.run(files["record"])
            ),
            patch.object(lifetime, "claim", side_effect=cancel_after_claim),
            patch.object(lifetime.ManagedJob, "start") as start,
        ):
            self.recruit()
        crew = self.crew()
        self.assertEqual(self.finished(crew)["outcome"], "cancelled")
        start.assert_not_called()
        ref = crew.record["lifetime_id"]
        self.assertEqual(lifetime.read(crew.session, ref)["state"], "prepared")
        self.assertTrue(lifetime.read(crew.session, ref)["revoked"])
        with self.assertRaisesRegex(CaptainError, "revoked"):
            with lifetime.claim(crew.session, ref):
                self.fail("cancelled launch was replayed")
        response = protocol.wait(crew, 0)
        protocol.wait(crew, 0, ack=response["delivery_id"])
        with contextlib.redirect_stdout(io.StringIO()):
            agents.dismiss_crew(self.args("dismiss", "jack"), self.pane, self.project)
        self.assertTrue(self.crew().is_dismissed)
        self.herdr.assert_not_called()

    def test_an_owner_that_never_starts_fails_the_recruit_and_frees_the_name(self):
        self.native("exit 0")
        with (
            patch.object(sys, "executable", "/usr/bin/false"),
            self.assertRaisesRegex(CaptainError, r"\[background-start\]"),
        ):
            self.recruit()
        crew = self.crew()
        self.assertTrue(crew.is_dismissed)
        self.assertEqual(self.assignment(crew)["outcome"], "failed")
        self.assertFalse(background.alive(crew))

    def test_follow_ups_focus_and_model_switches_are_refused(self):
        self.native(f"{SAY}")
        self.recruit()
        crew = self.crew()
        self.finished(crew)
        assignment = self.assignment(crew)["id"]
        for command, call in (
            (("tell", "jack", "more", "--assignment", assignment), agents.tell_crew),
            (("answer", "jack", "q1", "yes", "--assignment", assignment), agents.protocol_command),
            (("focus", "jack"), agents.focus_crew),
            (("model", "jack", "mid"), agents.switch_model),
        ):
            with self.subTest(command=command[0]):
                with self.assertRaisesRegex(CaptainError, r"\[background-unsupported\]"):
                    call(self.args(*command), self.pane, self.project)
        self.herdr.assert_not_called()

    def test_a_legacy_temporary_session_refuses_background_work(self):
        roots = {
            "CAPTAIN_STATE_ROOT": str(self.root / "durable"),
            "CAPTAIN_TEMP_ROOT": str(self.root / "temp"),
        }
        self.enterContext(patch.dict(os.environ, roots))
        legacy = store.storage(self.project) / "sessions" / self.meta["id"]
        legacy.mkdir(parents=True)
        store.write_json(legacy / "session.json", self.meta)
        self.native("exit 0")
        with self.assertRaisesRegex(CaptainError, r"\[legacy-session\]"):
            self.recruit()
        self.assertFalse((legacy / "protocol.json").exists())

    def managed_job(self):
        job = Mock()
        job.stdout = io.BufferedReader(
            io.BytesIO("\n".join(json.dumps(event) for event in REPORT).encode())
        )
        job.stderr = io.BufferedReader(io.BytesIO(b""))
        job.poll.return_value = 0
        job.native_returncode = 0
        job.shutdown.return_value = SimpleNamespace(ok=True, reason="stopped")
        job.finish.return_value = SimpleNamespace(ok=True, reason="complete")
        return job

    def execute_job(self, job):
        files = background.paths(self.root / "managed")
        record = {
            "timeout": 10,
            "provider": "codex",
            "model": None,
            "session": str(self.directory),
            "lifetime_id": "managed",
        }
        with patch.object(lifetime, "claim", return_value=contextlib.nullcontext(job)):
            return background._execute(files, record)

    def test_background_finishes_only_after_shutdown_and_output_readers(self):
        job = self.managed_job()

        def finish():
            job.shutdown.assert_called_once_with()
            self.assertTrue(job.stdout.closed)
            self.assertTrue(job.stderr.closed)
            return SimpleNamespace(ok=True, reason="complete")

        job.finish.side_effect = finish
        self.assertEqual(self.execute_job(job), ("done", "slow path is X"))
        job.finish.assert_called_once_with()
        job.invalidate.assert_not_called()

    def test_incomplete_shutdown_cannot_publish_a_background_receipt(self):
        job = self.managed_job()
        job.shutdown.return_value = SimpleNamespace(ok=False, reason="group still present")
        outcome, report = self.execute_job(job)
        self.assertEqual(outcome, "failed")
        self.assertIn("group still present", report)
        job.finish.assert_not_called()

    def test_escaped_pipe_holders_invalidate_cleanup_without_hanging(self):
        job = self.managed_job()
        with patch.object(background.threading, "Thread") as thread:
            thread.return_value.is_alive.return_value = True
            outcome, report = self.execute_job(job)
        self.assertEqual(outcome, "failed")
        self.assertIn("artifacts retained", report)
        job.invalidate.assert_called_once()
        job.finish.assert_not_called()
        for call in thread.return_value.join.call_args_list:
            self.assertGreaterEqual(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 5)
        job.stdout.close()
        job.stderr.close()

    def test_output_read_errors_prevent_a_background_receipt(self):
        job = self.managed_job()
        job.stdout.close()
        job.stdout = Mock()
        job.stdout.__enter__ = Mock(return_value=job.stdout)
        job.stdout.__exit__ = Mock(return_value=False)
        job.stdout.read1.side_effect = OSError("broken output")
        outcome, report = self.execute_job(job)
        self.assertEqual(outcome, "failed")
        self.assertIn("artifacts retained", report)
        job.invalidate.assert_called_once()
        job.finish.assert_not_called()

    def test_output_past_the_limit_is_drained_but_not_kept(self):
        path = self.root / "out"
        over = threading.Event()
        failed = threading.Event()
        background._copy(io.BufferedReader(io.BytesIO(b"x" * 100)), path, 10, over, failed)
        self.assertFalse(failed.is_set())
        self.assertEqual(path.read_bytes(), b"x" * 10)
        self.assertTrue(over.is_set())
