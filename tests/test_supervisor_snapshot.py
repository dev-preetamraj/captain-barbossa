"""snapshot: the app's read model, taken at one action-log cursor it can subscribe from."""

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

from captain_barbossa import (
    captain_bridge,
    config,
    do,
    eventlog,
    protocol,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


class SupervisorSnapshotTests(SessionCase):
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
        adapter = types.SimpleNamespace(
            open=lambda link: None,
            state=lambda link: "working",
            ring=lambda link, message_id, text: {"landed": False, "gate": "agent not idle"},
            interrupt=lambda link, reason: "idle",
            set_model=lambda link, model: {"model": model, "applies": "now"},
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
        record = {"id": "will", "agent": "c-will", "incarnation_id": "b" * 32, "provider": "codex"}
        record.update(execution="supervised", checkout="shared", model="gpt-5")
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        self.current = self.current._replace(meta=meta)
        self.crew = Crew("will", record, self.current)
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

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def test_the_snapshot_holds_every_agent_and_open_item(self):
        assignment = protocol.begin(self.crew, self.project, "Map the repo.")["id"]
        protocol.once(self.crew, "hooks_untrusted", "[codex-hooks-untrusted] Trust them once.")
        directory = self.current.directory
        row = do.request_authorization(directory, do.parse(["do", "push"]), "app", ["do", "push"])
        self.call("start_captain", {"provider": "fake", "model": "m1", "effort": "high"})
        asked = captain_bridge.open_request(directory, "approval", {"tool": "Bash"})
        self.call("message_captain", {"text": "Plan it.", "message_id": "m1"})
        self.call("rename", {"name": "Release prep"})
        found = self.call("snapshot")
        self.assertEqual(found["session_id"], self.current.meta["id"])
        self.assertEqual(found["name"], "Release prep")
        self.assertIn("actions", found["cursors"])
        captain = found["captain"]
        self.assertEqual(
            (captain["provider"], captain["model"], captain["effort"], captain["state"]),
            ("fake", "m1", "high", "working"),
        )
        (agent,) = found["agents"]
        self.assertEqual(
            {key: agent[key] for key in ("id", "provider", "model", "incarnation", "checkout")},
            {
                "id": "will",
                "provider": "codex",
                "model": "gpt-5",
                "incarnation": "b" * 32,
                "checkout": "shared",
            },
        )
        self.assertIn("state", agent)
        (listed,) = found["assignments"]
        self.assertEqual(
            (listed["id"], listed["task"], listed["active"]), (assignment, "Map the repo.", True)
        )
        self.assertEqual([d["status"] for d in found["deliveries"]], ["error"])
        self.assertEqual([r["id"] for r in found["authorizations"]], [row["id"]])
        self.assertEqual([r["id"] for r in found["captain_requests"]], [asked["id"]])
        self.assertEqual([p["id"] for p in found["prompts"]], ["m1"])
        self.assertIn("agents", found["usage"])
        for key in ("mail", "questions", "grant_requests", "results", "uncertain_deliveries"):
            self.assertIsInstance(found[key], list, key)

    def test_actions_written_during_the_read_leave_no_gap_before_the_cursor(self):
        directory, done, written = self.current.directory, threading.Event(), []

        def writer():
            number = 0
            while not done.is_set():
                captain_bridge.queue_message(directory, f"p{number}", f"Prompt {number}.")
                written.append(f"p{number}")
                number += 1

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            snaps = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline or len(snaps) < 5:
                snaps.append(self.call("snapshot"))
        finally:
            done.set()
            thread.join(10)
        eventlog.flush_actions(directory)
        log = (directory / eventlog.ACTIONS).read_bytes()
        queued = []
        offset = 0
        for line in log.splitlines(keepends=True):
            action = json.loads(line)
            if action["kind"] == "prompt" and action["state"] == "queued":
                queued.append((offset, action["message"]))
            offset += len(line)
        self.assertEqual([message for _, message in queued], written)  # none lost or repeated
        for snap in snaps:
            cursor = snap["cursors"]["actions"]["offset"]
            self.assertTrue(cursor == 0 or log[cursor - 1 : cursor] == b"\n")
            before = {message for at, message in queued if at < cursor}
            shown = {row["id"] for row in snap["prompts"]}
            self.assertLessEqual(before, shown)  # every line before the cursor is in the read
