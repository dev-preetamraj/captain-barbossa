"""A supervised Grok crew runs its assignment over ACP with Herdr unreachable."""

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
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    agents,
    cli,
    config,
    grok_acp,
    lifetime,
    models,
    runtime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew

FAKE = Path(__file__).with_name("fake_grok_acp.py")
MODEL = "grok-4.6"
NEXT_MODEL = "grok-4.7"
FLAGGED = "git push origin main"


class SupervisedGrokTests(SessionCase):
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
        grok = self.root / "bin" / "grok"
        grok.parent.mkdir()
        grok.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        grok.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"FAKE_GROK_DIR": str(self.fake)}))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(agents, "executable", return_value=str(grok)))
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"grok": grok_acp}))
        self.herdr = self.enterContext(
            patch.object(runtime, "herdr", side_effect=AssertionError("Herdr was called"))
        )
        self.current = supervisor.open_session(self.project, None)
        self.serve()

    def serve(self):
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        self.stopped, self.thread = stopped, thread
        with os.fdopen(read_fd) as lines:
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]

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

    def inbound(self, method):
        return [entry["in"] for entry in self.log() if entry.get("in", {}).get("method") == method]

    def permissions(self):
        return [
            entry["permission_response"] for entry in self.log() if "permission_response" in entry
        ]

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

    def test_assign_tell_interrupt_model_permission_done_dismiss(self):
        printed = self.captain(
            "crew",
            "jack",
            "--agent",
            "grok",
            "--model",
            MODEL,
            "--task",
            "Edit notes.md and report.",
            "--owns",
            "notes.md",
            "--allow",
            "edit",
        )
        record = json.loads(printed.splitlines()[-1])
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        assignment = record["assignment_id"]
        argv = self.until(
            lambda: next((entry["argv"] for entry in self.log() if "argv" in entry), None),
            "the Grok argv",
        )
        self.assertIn("--no-subagents", argv)
        created = self.inbound("session/new")[0]
        self.assertEqual(created["params"], {"cwd": str(self.project), "mcpServers": []})
        first = self.until(lambda: self.inbound("session/prompt"), "the assignment prompt")[0]
        self.assertIn("Edit notes.md and report.", first["params"]["prompt"][0]["text"])
        self.assertIn("inbox jack", first["params"]["prompt"][0]["text"])

        self.captain("tell", "jack", "Also add a title.", "--assignment", assignment)
        self.assertEqual(len(self.inbound("session/prompt")), 1)
        stamp = store.read_json(self.current.directory / "mail" / "jack" / ".drain")
        self.assertEqual((stamp["landed"], stamp["gate"]), (False, "agent not idle"))
        self.control(do="complete", reply="Edited.")
        second = self.until(lambda: self.inbound("session/prompt")[1:], "the held prompt")[0]
        self.assertIn("Also add a title.", second["params"]["prompt"][0]["text"])

        self.assertIn("Interrupted Jack.", self.captain("interrupt", "jack", "--reason", "stop"))
        self.assertEqual(len(self.inbound("session/cancel")), 1)
        self.idle()

        switched = self.captain("model", "jack", NEXT_MODEL)
        self.assertIn(f"switched to {NEXT_MODEL}", switched)
        changed = self.inbound("session/set_model")[-1]
        self.assertEqual(changed["params"]["modelId"], NEXT_MODEL)
        self.assertEqual(self.crew().record["model"], NEXT_MODEL)
        self.captain("tell", "jack", "Carry on with the new model.", "--assignment", assignment)
        self.until(lambda: self.inbound("session/prompt")[2:], "the retiered prompt")

        self.control(do="permission", command=FLAGGED)
        denied = self.until(self.permissions, "the denied permission")[-1]
        self.assertEqual(
            denied["result"],
            {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
        )
        self.captain("grant", "jack", "--command", FLAGGED, "--assignment", assignment)
        self.control(do="permission", command=FLAGGED)
        allowed = self.until(lambda: self.permissions()[1:], "the allowed permission")[-1]
        self.assertEqual(
            allowed["result"],
            {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
        )
        self.assertEqual(self.assignment(assignment).get("grants"), [])
        self.control(do="permission", command=FLAGGED)
        denied_again = self.until(lambda: self.permissions()[2:], "the one-shot denial")[-1]
        self.assertEqual(
            denied_again["result"],
            {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
        )

        self.control(do="complete", reply="Done.")
        self.idle()
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
        self.assertEqual(lifetime.read(self.current, crew.record["lifetime_id"])["state"], "exited")
        self.herdr.assert_not_called()

    def test_dismiss_cancels_an_active_prompt_before_closing_stdin(self):
        printed = self.captain(
            "crew",
            "jack",
            "--agent",
            "grok",
            "--model",
            MODEL,
            "--task",
            "Wait for dismissal.",
        )
        record = json.loads(printed.splitlines()[-1])
        self.until(lambda: self.inbound("session/prompt"), "the active prompt")

        self.assertIn("Dismissed Jack.", self.captain("dismiss", "jack", "--force"))

        self.assertEqual(len(self.inbound("session/cancel")), 1, self.log())
        launch = lifetime.read(self.current, record["lifetime_id"])
        self.assertEqual(launch["state"], "exited")
        self.herdr.assert_not_called()

    def test_verified_version_floor(self):
        self.assertFalse(models.grok_acp_supported("1.0.61"))
        self.assertTrue(models.grok_acp_supported("1.0.62"))
        self.assertTrue(models.grok_acp_supported("grok 1.1.0"))
