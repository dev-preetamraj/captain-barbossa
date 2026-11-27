"""The supervised captain's model and effort: recorded in captain.json, reused on a restart,
switched with set_captain_model, and announced as a lifecycle action when they change."""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, eventlog, store, supervision, supervisor
from captain_barbossa.runtime import CaptainError

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]
COMMANDS = {"commands": [{"name": "compact", "description": "Compact."}], "skills": []}


def fake_adapter(opened):
    def open_link(link):
        opened.append((link.model, link.effort))

    def set_model(link, model, effort=None):
        link.model = model or link.model
        link.effort = effort or link.effort
        return {"model": link.model, "effort": link.effort, "applies": "now"}

    return types.SimpleNamespace(
        open=open_link,
        state=lambda link: "idle",
        ring=lambda link, message_id, text: {"landed": True, "gate": None},
        interrupt=lambda link, reason: "idle",
        set_model=set_model,
        current=lambda link: {"model": link.model or "native-default", "effort": link.effort},
        commands=lambda link: COMMANDS,
    )


class CaptainModelStateTests(SessionCase):
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
        self.opened = []
        adapters = {"fake": fake_adapter(self.opened), "other": fake_adapter(self.opened)}
        self.enterContext(patch.dict(supervisor.ADAPTERS, adapters))
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
        self.enterContext(
            patch.object(supervisor, "resolve_model", side_effect=lambda p, text: f"id-{text}")
        )
        self.enterContext(patch.object(config, "text", side_effect=lambda *key: None))
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

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def record(self):
        return store.read_json(self.current.directory / "captain.json")

    def changes(self):
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        return [
            (action["model"], action["effort"])
            for action in actions
            if action["kind"] == "lifecycle" and action["state"] == "model-changed"
        ]

    def test_the_native_default_is_recorded_and_a_restart_of_the_same_provider_reuses_it(self):
        started = self.call("start_captain", {"provider": "fake", "effort": "high"})
        self.assertEqual((started["model"], started["effort"]), ("native-default", "high"))
        self.assertEqual(
            (self.record()["model"], self.record()["effort"]), ("native-default", "high")
        )
        self.call("stop_captain")
        stopped = self.call("captain_state")
        self.assertEqual(
            stopped,
            {
                "state": None,
                "provider": "fake",
                "model": "native-default",
                "effort": "high",
                "permission_mode": None,
            },
        )
        self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.opened[-1], ("native-default", "high"))
        self.call("stop_captain")
        self.call("start_captain", {"provider": "other"})
        self.assertEqual(self.opened[-1], (None, None))
        self.assertEqual(self.changes(), [])

    def test_set_captain_model_switches_records_and_announces_a_change_once(self):
        self.call("start_captain", {"provider": "fake", "model": "m1"})
        changed = self.call("set_captain_model", {"model": "fast", "effort": "low"})
        self.assertEqual(changed, {"model": "id-fast", "effort": "low", "applies": "now"})
        self.assertEqual((self.record()["model"], self.record()["effort"]), ("id-fast", "low"))
        self.call("set_captain_model", {"effort": "low"})
        self.assertEqual(self.changes(), [("id-fast", "low")])
        state = self.call("captain_state")
        self.assertEqual((state["model"], state["effort"]), ("id-fast", "low"))
        for params in ({}, {"effort": 3}):
            with self.subTest(params=params):
                with self.assertRaisesRegex(CaptainError, r"\[supervisor-params\]"):
                    self.call("set_captain_model", params)
        self.call("stop_captain")
        with self.assertRaisesRegex(CaptainError, r"\[captain-not-running\]"):
            self.call("set_captain_model", {"effort": "high"})

    def test_captain_commands_come_from_the_native(self):
        with self.assertRaisesRegex(CaptainError, r"\[captain-not-running\]"):
            self.call("captain_commands")
        self.call("start_captain", {"provider": "fake"})
        compact = {**COMMANDS["commands"][0], "argument_hint": None}
        self.assertEqual(self.call("captain_commands"), {"commands": [compact], "skills": []})


if __name__ == "__main__":
    unittest.main()
