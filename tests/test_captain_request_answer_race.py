"""A captain request is answerable from the moment it is listed: its answer slot is registered
before the request reaches captain_requests, the snapshot or the action log."""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import captain_bridge, config, store, supervision, supervisor

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


class CaptainRequestAnswerRaceTests(SessionCase):
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
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.holder = {}
        adapter = types.SimpleNamespace(
            open=lambda link: self.holder.update(link=link),
            state=lambda link: "idle",
            ring=lambda link, message_id, text: {"landed": True, "gate": None},
            interrupt=lambda link, reason: "idle",
        )
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": adapter}))
        self.enterContext(
            patch.object(
                supervisor,
                "captain_command",
                side_effect=lambda sup, provider, model, effort: (
                    NATIVE,
                    dict(os.environ),
                    model,
                    "rpc",
                ),
            )
        )
        self.current = supervisor.open_session(self.project, None)
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]
        self.call("start_captain", {"provider": "fake"})

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def test_the_slot_exists_when_the_request_is_written(self):
        seen = []
        opened = captain_bridge.open_request

        def spy(directory, kind, payload, request_id=None):
            seen.append(self.call("captain_state")["state"] == "blocked")
            return opened(directory, kind, payload, request_id)

        with patch.object(captain_bridge, "open_request", side_effect=spy):
            self.answered_on_first_sight()
        self.assertEqual(seen, [True])

    def test_an_answer_sent_on_first_sight_always_lands(self):
        for _ in range(10):
            self.assertEqual(self.answered_on_first_sight(), "approve")

    def answered_on_first_sight(self):
        link = self.holder["link"]
        replies = []
        payload = {"tool": "Bash", "command": "make test", "cwd": "/w"}
        threading.Thread(
            target=lambda: replies.append(link.ask("approval", payload)), daemon=True
        ).start()
        known = {row["id"] for row in self.call("captain_requests")["requests"]}
        deadline = time.monotonic() + 10
        while True:
            rows = self.call("captain_requests")["requests"]
            fresh = [row for row in rows if row["id"] not in known and row["state"] == "open"]
            if fresh:
                break
            self.assertLess(time.monotonic(), deadline)
        reply = self.call("answer_captain", {"request": fresh[0]["id"], "decision": "approve"})
        self.assertEqual(reply["state"], "answered")
        while not replies:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        return replies[0]
