"""A supervised pi crew runs its assignment over RPC with Herdr unreachable."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    agents,
    cli,
    config,
    instructions,
    lifetime,
    models,
    pi_rpc,
    runtime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew

FAKE = Path(__file__).with_name("fake_pi_rpc.py")
MODEL = "anthropic/claude-sonnet-5-5"
NEXT_MODEL = "anthropic/claude-opus-5-5"
FLAGGED = "git push origin main"
PI_MODELS = (
    ("anthropic/claude-haiku-5-5", ("claude-haiku-5-5",)),
    (MODEL, ("claude-sonnet-5-5",)),
    (NEXT_MODEL, ("claude-opus-5-5",)),
)


class SupervisedPiTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=0.5\nshutdown_timeout=2.0\n"
            "[supervisor]\nrequest_timeout=10.0\nstartup_timeout=15.0\nstop_timeout=10.0\n"
            "hook_timeout=20.0\n",
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.fake = self.root / "fake"
        (self.fake / "control").mkdir(parents=True)
        pi = self.root / "bin" / "pi"
        pi.parent.mkdir()
        pi.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        pi.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"FAKE_PI_DIR": str(self.fake)}))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(agents, "executable", return_value=str(pi)))
        self.enterContext(patch.object(models, "pi_models", return_value=PI_MODELS))
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"pi": pi_rpc}))
        self.herdr = self.enterContext(
            patch.object(runtime, "herdr", side_effect=AssertionError("Herdr was called"))
        )
        self.current = supervisor.open_session(self.project, None)
        self.serve()

    def serve(self):
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()
        created = threading.Event()
        supervisor_class = supervisor.Supervisor

        def factory(*args, **kwargs):
            self.app = supervisor_class(*args, **kwargs)
            created.set()
            return self.app

        def target():
            with (
                patch.object(supervisor, "Supervisor", side_effect=factory),
                os.fdopen(write_fd, "w") as out,
            ):
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        self.stopped, self.thread = stopped, thread
        with os.fdopen(read_fd) as lines:
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]
        self.assertTrue(created.wait(5))

    def captain(self, *argv, env=None, code=0):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env or {}),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            returned = cli.main(["--session", self.current.meta["id"], *argv])
        self.assertEqual(returned, code, errors.getvalue())
        return output.getvalue() if code == 0 else errors.getvalue()

    def log(self):
        path = self.fake / "log.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line) for line in lines]

    def inbound(self, kind):
        return [entry["in"] for entry in self.log() if entry.get("in", {}).get("type") == kind]

    def hooks(self):
        return [entry["output"] for entry in self.log() if entry.get("hook") == "PreToolUse"]

    def until(self, check, what):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if found := check():
                return found
            time.sleep(0.05)
        self.fail(
            f"timed out waiting for {what}: {supervision.state(self.crew())} "
            f"{json.dumps(self.log())[-3000:]}"
        )

    def control(self, **command):
        name = f"{time.monotonic_ns()}.json"
        staging = self.fake / name
        staging.write_text(json.dumps(command))
        staging.rename(self.fake / "control" / name)
        self.until(
            lambda: any(entry.get("control") == command for entry in self.log()),
            f"the {command['do']} control",
        )

    def link(self):
        with self.app.lock:
            return self.app.links["jack"]

    def recruit(self):
        printed = self.captain(
            "crew", "jack", "--agent", "pi", "--model", MODEL,
            "--task", "Test the pi adapter.", "--owns", "notes.md", "--allow", "edit",
        )  # fmt: skip
        record = json.loads(printed.splitlines()[-1])
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        self.addCleanup(supervisor.stop, self.app, {"crew": "jack"})
        self.until(lambda: self.inbound("prompt"), "the assignment prompt")
        return record["assignment_id"]

    def complete(self):
        self.control(do="complete")
        self.idle()

    def crew(self):
        meta = store.read_json(self.current.meta_path)
        return Crew("jack", meta["crew"]["jack"], sessions.Session(self.current.directory, meta))

    def assignment(self, assignment_id):
        return store.read_json(self.current.directory / "protocol.json")["assignments"][
            assignment_id
        ]

    def as_crew(self, assignment):
        return {
            "CAPTAIN_ROLE": "crew",
            "CAPTAIN_CREW": "jack",
            "CAPTAIN_INCARNATION": self.crew().record["incarnation_id"],
            "CAPTAIN_ASSIGNMENT": assignment,
            supervision.TOKEN_ENV: "",
        }

    def idle(self):
        self.until(lambda: supervision.state(self.crew()) == "idle", "an idle crew")

    def test_ring_get_state_failure_releases_starting_slot(self):
        self.recruit()
        self.complete()
        self.control(do="state_error", error="state unavailable")
        link = self.link()

        with self.assertRaisesRegex(runtime.CaptainError, "state unavailable"):
            pi_rpc.ring(link, "failed-state", "Try once.")

        self.assertIsNone(link.turn)
        self.assertNotIn("sending", link.native)
        self.assertEqual(
            pi_rpc.ring(link, "retry-state", "Try again."),
            {"landed": True, "gate": None},
        )
        self.complete()

    def test_ring_not_idle_does_not_accept_undelivered_mail(self):
        self.recruit()
        self.complete()
        self.control(do="streaming", value=True)
        link = self.link()

        self.assertEqual(
            pi_rpc.ring(link, "busy-message", "Wait for idle."),
            {"landed": False, "gate": "agent not idle"},
        )
        self.assertNotIn("sending", link.native)
        with link.guard:
            pi_rpc._settled(link)
        self.assertNotIn("busy-message", link.native["accepted"])
        self.control(do="streaming", value=False)
        self.assertEqual(
            pi_rpc.ring(link, "busy-message", "Now deliver."),
            {"landed": True, "gate": None},
        )
        self.complete()

    def test_interrupt_starting_and_finishing_are_idle(self):
        self.recruit()
        link = self.link()
        active = self.until(
            lambda: link.turn if link.turn not in (None, pi_rpc.STARTING) else None,
            "an active pi turn",
        )

        for marker in (pi_rpc.STARTING, pi_rpc.FINISHING):
            with self.subTest(turn=marker):
                with link.guard:
                    link.turn = marker
                self.assertEqual(pi_rpc.interrupt(link, "stop"), "idle")
        self.assertEqual(self.inbound("abort"), [])
        with link.guard:
            link.turn = active
        self.complete()

    def test_extension_dialogs_are_cancelled(self):
        self.recruit()

        for method in ("select", "confirm", "input", "editor"):
            with self.subTest(method=method):
                identifier = f"ui-{method}"
                self.control(do="extension_ui", id=identifier, method=method)
                response = self.until(
                    lambda: next(
                        (
                            item
                            for item in self.inbound("extension_ui_response")
                            if item.get("id") == identifier
                        ),
                        None,
                    ),
                    f"the {method} cancellation",
                )
                self.assertEqual(
                    response,
                    {"type": "extension_ui_response", "id": identifier, "cancelled": True},
                )
        self.complete()

    def test_open_uses_startup_timeout_for_first_state(self):
        class StubPeer:
            def __init__(self):
                self.requests = []
                self.started = False

            def start(self):
                self.started = True

            def request(self, command, params, timeout):
                self.requests.append((command, params, timeout))
                return {"isStreaming": False, "sessionId": "session-1"}

        peer = StubPeer()
        link = SimpleNamespace(stdout=11, stdin=12, crew_id="jack", thread=None)
        with patch.object(pi_rpc, "Peer", return_value=peer):
            pi_rpc.open(link)

        self.assertTrue(peer.started)
        self.assertEqual(peer.requests, [("get_state", {}, 15.0)])
        self.assertEqual((link.stdin, link.stdout, link.thread), (None, None, "session-1"))

    def test_close_aborts_active_turn_before_stdin_closes(self):
        self.recruit()
        link = self.link()
        self.until(
            lambda: link.turn not in (None, pi_rpc.STARTING),
            "an active pi turn",
        )

        pi_rpc.close(link)

        self.until(lambda: self.inbound("abort"), "the shutdown abort")
        self.idle()

    def test_assign_tell_interrupt_retier_grant_done_dismiss(self):
        printed = self.captain(
            "crew", "jack", "--agent", "pi", "--model", MODEL,
            "--task", "Edit notes.md and report.", "--owns", "notes.md", "--allow", "edit",
        )  # fmt: skip
        record = json.loads(printed.splitlines()[-1])
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        assignment = record["assignment_id"]
        argv = self.until(
            lambda: next((entry["argv"] for entry in self.log() if "argv" in entry), None),
            "the pi argv",
        )
        self.assertEqual(argv[:2], ["--mode", "rpc"])
        self.assertIn("--extension", argv)
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertEqual(instructions.native_command("/bin/pi"), ["/bin/pi"])
        first = self.until(lambda: self.inbound("prompt"), "the assignment prompt")[0]
        self.assertIn("Edit notes.md and report.", first["message"])
        self.assertIn("inbox jack", first["message"])

        self.captain("tell", "jack", "Also add a title.", "--assignment", assignment)
        self.assertEqual(len(self.inbound("prompt")), 1)
        stamp = store.read_json(self.current.directory / "mail" / "jack" / ".drain")
        self.assertEqual((stamp["landed"], stamp["gate"]), (False, "agent not idle"))
        self.control(do="complete", reply="Edited.")
        second = self.until(lambda: self.inbound("prompt")[1:], "the held prompt")[0]
        self.assertIn("Also add a title.", second["message"])
        events = [json.loads(line) for line in self.crew().events.read_text().splitlines()]
        completed = [event for event in events if event.get("type") == "agent-turn-complete"]
        self.assertEqual(completed[0]["last-assistant-message"], "Edited.")

        self.assertIn("Interrupted Jack.", self.captain("interrupt", "jack", "--reason", "stop"))
        self.assertEqual(len(self.inbound("abort")), 1)
        self.idle()

        switched = self.captain("model", "jack", NEXT_MODEL)
        self.assertIn(f"switched to {NEXT_MODEL}", switched)
        changed = self.inbound("set_model")[-1]
        self.assertEqual((changed["provider"], changed["modelId"]), tuple(NEXT_MODEL.split("/", 1)))
        self.assertEqual(self.crew().record["model"], NEXT_MODEL)
        self.captain("tell", "jack", "Carry on with the new model.", "--assignment", assignment)
        self.until(lambda: self.inbound("prompt")[2:], "the retiered prompt")

        self.control(do="tool", command=FLAGGED, cwd=str(self.project))
        self.until(self.hooks, "the denied PreToolUse")
        denied = self.hooks()[-1]["hookSpecificOutput"]
        self.assertEqual(denied["permissionDecision"], "deny")
        asked = json.loads(
            self.captain("ask", "jack", "I need to push to main.", env=self.as_crew(assignment))
        )
        request = self.assignment(assignment)["grant_requests"][0]
        self.assertEqual(asked["grant_request"], request["id"])
        self.assertEqual((request["command"], request["code"]), (FLAGGED, "flagged-outward"))
        resolved = supervision.resolve_grant(self.current.directory, request["id"], "grant")
        self.assertEqual(resolved["state"], "granted")
        self.control(do="complete", reply="Asked.")
        fourth = self.until(lambda: self.inbound("prompt")[3:], "the grant answer")[0]
        self.assertIn("Granted once", fourth["message"])
        self.control(do="tool", command=FLAGGED, cwd=str(self.project))
        self.until(lambda: len(self.hooks()) == 2, "the granted PreToolUse")
        allowed = self.hooks()[-1]["hookSpecificOutput"]
        self.assertEqual(allowed["permissionDecision"], "allow")
        self.assertEqual(self.assignment(assignment).get("grants"), [])

        outcomes = supervision.interrupt_all(self.current.directory, "stop everything")
        self.assertEqual(outcomes, [{"crew": "jack", "outcome": "interrupted"}])
        self.assertEqual(len(self.inbound("abort")), 2)
        self.idle()
        self.assertEqual(
            supervision.interrupt_all(self.current.directory),
            [{"crew": "jack", "outcome": "idle"}],
        )

        crew = self.crew()
        self.captain("done", "jack", "--report", "Edited notes.md.", env=self.as_crew(assignment))
        statuses, ack = [], []
        for _ in range(20):
            response = json.loads(self.captain("wait", "jack", "--json", "--timeout", "0", *ack))
            if not response["delivery_id"]:
                break
            statuses.append(response["status"])
            ack = ["--ack", response["delivery_id"]]
        self.assertEqual(statuses[-1], "done")
        self.assertIn("Dismissed Jack.", self.captain("dismiss", "jack", "--confirm-unlanded"))
        launch = lifetime.read(self.current, crew.record["lifetime_id"])
        self.assertEqual(launch["state"], "exited")
        self.herdr.assert_not_called()
