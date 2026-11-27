"""A supervised Codex captain resumes its recorded app-server thread."""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, store, supervision, supervisor

FAKE = Path(__file__).with_name("fake_codex_app_server.py")
RECORDED_THREAD = "11111111-1111-1111-1111-111111111111"
STARTED_THREAD = "22222222-2222-2222-2222-222222222222"


class SupervisedCodexCaptainResumeTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=2.0\n",
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
        self.resume = "accept"
        self.enterContext(patch.dict(os.environ))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(supervisor, "captain_command", side_effect=self.command))
        self.current = supervisor.open_session(self.project, None)
        self.current.events("captain").parent.mkdir(parents=True, exist_ok=True)
        read_fd, write_fd = os.pipe()
        self.stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, self.stopped)

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 30)
        self.addCleanup(self.stopped.set)
        with os.fdopen(read_fd) as lines:
            ready = json.loads(lines.readline())
        os.environ[supervision.TOKEN_ENV] = ready["token"]

    def command(self, supervisor_, provider, model, effort):
        env = dict(os.environ)
        env.update(
            FAKE_CODEX_DIR=str(self.fake),
            FAKE_CODEX_RESUME=self.resume,
            FAKE_CODEX_THREAD_ID=STARTED_THREAD,
        )
        return [sys.executable, str(FAKE), "app-server"], env, model, "app-server"

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=20)

    def record_thread(self):
        path = self.current.events("captain")
        path.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            path,
            json.dumps({"type": "agent-turn-complete", "thread-id": RECORDED_THREAD}) + "\n",
        )

    def inbound(self, method):
        path = self.fake / "log.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row["in"] for row in rows if row.get("in", {}).get("method") == method]

    def control(self, **command):
        name = f"{time.monotonic_ns()}.json"
        staging = self.fake / name
        store.write_text(staging, json.dumps(command))
        staging.rename(self.fake / "control" / name)

    def recorded_threads(self):
        path = self.current.events("captain")
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row.get("thread-id") for row in rows if row.get("thread-id")]

    def until(self, check, what):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if found := check():
                return found
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def test_completed_thread_is_resumed_with_the_current_captain_settings(self):
        settings = {"provider": "codex", "model": "gpt-test"}
        self.call("start_captain", settings)
        self.call("message_captain", {"text": "Remember this."})
        self.until(lambda: self.inbound("turn/start"), "the first captain turn")
        self.control(do="complete", reply="Remembered.")
        self.until(lambda: STARTED_THREAD in self.recorded_threads(), "the recorded thread")
        self.call("stop_captain")
        self.call("start_captain", settings)
        resumed = self.until(lambda: self.inbound("thread/resume"), "thread/resume")
        self.assertEqual(
            resumed[-1]["params"],
            {
                "cwd": str(self.project),
                "model": "gpt-test",
                "threadId": STARTED_THREAD,
                "excludeTurns": True,
            },
        )
        self.assertEqual(len(self.inbound("thread/start")), 1)

    def test_refused_resume_reports_attention_before_starting_a_new_thread(self):
        self.record_thread()
        self.resume = "refuse"
        with patch.object(supervisor, "attention") as attention:
            self.call("start_captain", {"provider": "codex"})
        methods = [
            row["in"]["method"]
            for row in [
                json.loads(line) for line in (self.fake / "log.jsonl").read_text().splitlines()
            ]
            if "in" in row and row["in"]["method"].startswith("thread/")
        ]
        self.assertEqual(methods, ["thread/resume", "thread/start"])
        attention.assert_called_once()
        self.assertEqual(attention.call_args.args[1], "captain")
        self.assertRegex(attention.call_args.args[2], r"^\[codex-thread-resume\]")

    def test_captain_without_a_recorded_thread_starts_one(self):
        self.call("start_captain", {"provider": "codex"})
        started = self.until(lambda: self.inbound("thread/start"), "thread/start")
        self.assertEqual(started[-1]["params"], {"cwd": str(self.project)})
        self.assertEqual(self.inbound("thread/resume"), [])


if __name__ == "__main__":
    unittest.main()
