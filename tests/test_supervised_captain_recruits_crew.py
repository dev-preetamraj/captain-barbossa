"""A crew the supervised captain recruits is reachable from its first ring onward."""

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
    models,
    pi_rpc,
    protocol,
    runtime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError

FAKE = Path(__file__).with_name("fake_pi_rpc.py")
MODEL = "anthropic/claude-sonnet-5-5"
TASK = "Trace one assignment from launch to report."


class SupervisedCaptainRecruitsCrewTests(SessionCase):
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
        self.enterContext(patch.object(models, "pi_models", return_value=((MODEL, ()),)))
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"pi": pi_rpc}))
        self.enterContext(
            patch.object(runtime, "herdr", side_effect=AssertionError("Herdr was called"))
        )
        self.current = supervisor.open_session(self.project, None)
        self.serve()

    def serve(self):
        read_fd, write_fd = os.pipe()
        stopped, created = threading.Event(), threading.Event()
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
        with os.fdopen(read_fd) as lines:
            lines.readline()
        self.assertTrue(created.wait(5))
        # The captain CLI holds only the scoped token, as under start_captain.
        os.environ[supervision.TOKEN_ENV] = self.app.captain_token

    def captain(self, *argv, env=None):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"CAPTAIN_ROLE": "captain", **(env or {})}),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            returned = cli.main(["--session", self.current.meta["id"], *argv])
        self.assertEqual(returned, 0, errors.getvalue())
        return output.getvalue()

    def recruit(self):
        printed = self.captain("crew", "--agent", "pi", "--model", MODEL, "--task", TASK)
        record = json.loads(printed.splitlines()[-1])
        self.addCleanup(supervisor.stop, self.app, {"crew": record["id"]})
        return record

    def prompts(self, count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            path = self.fake / "log.jsonl"
            lines = path.read_text().splitlines() if path.exists() else []
            found = [
                entry["in"]["message"]
                for entry in map(json.loads, lines)
                if entry.get("in", {}).get("type") == "prompt"
            ]
            if len(found) >= count:
                return found
            time.sleep(0.05)
        self.fail(f"timed out waiting for {count} prompts")

    def crew(self, name="jack"):
        meta = store.read_json(self.current.meta_path)
        return Crew(name, meta["crew"][name], sessions.Session(self.current.directory, meta))

    def mail(self, assignment_id):
        state = store.read_json(self.current.directory / "protocol.json")
        initial = state["assignments"][assignment_id]["messages"][0]["id"]
        return store.read_json(
            protocol.mail_dir(self.current.directory, "jack") / f"{initial}.json"
        )

    def test_a_bridge_poll_during_launch_never_bounces_the_assignment(self):
        start = supervision.start

        def bridge_tick_then_start(crew, lifetime_ref):
            with supervision.acting_as(self.app.token):
                protocol.wait_all(self.current, 0)
            return start(crew, lifetime_ref)

        with patch.object(supervision, "start", side_effect=bridge_tick_then_start):
            record = self.recruit()
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        with self.app.lock:
            self.assertIn("jack", self.app.links)
        self.assertNotEqual(self.mail(record["assignment_id"])["state"], "bounced")
        self.assertIn(TASK, self.prompts(1)[0])

    def test_a_tell_after_a_bounce_redelivers_the_original_assignment(self):
        ring = supervision.ring
        refusals = [CaptainError("[supervisor-crew] jack is not running under this supervisor.")]

        def refuse_once(crew, message_id, text):
            if refusals:
                raise refusals.pop()
            return ring(crew, message_id, text)

        with patch.object(supervision, "ring", side_effect=refuse_once):
            record = self.recruit()
        assignment = record["assignment_id"]
        self.assertEqual(self.mail(assignment)["state"], "bounced")
        self.captain("tell", "jack", "Please pick up the assignment.", "--assignment", assignment)
        first = self.prompts(1)[0]
        self.assertIn(TASK, first)
        self.assertLess(first.index(TASK), first.index("Please pick up the assignment."))
        self.assertEqual(self.mail(assignment)["state"], "queued")
        inbox = self.captain(
            "inbox",
            "jack",
            env={
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": self.crew().record["incarnation_id"],
                "CAPTAIN_ASSIGNMENT": assignment,
                supervision.TOKEN_ENV: "",
            },
        )
        self.assertIn(TASK, inbox)
        self.assertIn("Please pick up the assignment.", inbox)


class HerdrBounceTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        record = {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "a"}
        self.crew = Crew("jack", record, self.current)
        self.current.meta["crew"]["jack"] = record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(self.crew, self.project, TASK, (), ("read",))

    def test_a_herdr_crew_bounce_is_not_requeued_by_a_tell(self):
        original = protocol.deliver(self.crew, TASK, initial=True, queue_only=True)
        protocol.bounce(self.crew, original, "pane gone")
        nudges = []
        pane = SimpleNamespace(
            nudge_block=lambda crew: None, nudge=lambda crew, text: nudges.append(text)
        )
        with patch.object(Crew, "pane", new=pane):
            protocol.deliver(self.crew, "Please pick up the assignment.")
        mail = protocol.mail_dir(self.current.directory, "jack") / f"{original}.json"
        self.assertEqual(store.read_json(mail)["state"], "bounced")
        self.assertNotIn("requeued_at", store.read_json(mail))
        (nudge,) = nudges
        self.assertNotIn(TASK, nudge)
        self.assertIn("Please pick up the assignment.", nudge)
