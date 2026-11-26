"""hello and command: the client surface names its protocol and capabilities, and runs only
whitelisted captain verbs, as the captain, within each verb's deadline."""

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, jsonrpc, store, supervision, supervisor


class SupervisorCommandWhitelistTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=5.0\n",
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: self.sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
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
            self.ready = json.loads(lines.readline())

    def call(self, method, params, timeout=60):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(self.ready["socket"])
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": {"token": self.ready["token"], **params},
            }
            client.sendall(json.dumps(request).encode() + b"\n")
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + timeout)

    def test_hello_names_protocol_session_and_provider_capabilities(self):
        hello = self.call("hello", {})["result"]
        self.assertEqual(hello["protocol"], "captain.supervisor/1")
        self.assertEqual(hello["session_id"], self.current.meta["id"])
        self.assertEqual(set(hello["capabilities"]), {"codex", "claude", "pi", "grok"})
        self.assertTrue(hello["capabilities"]["codex"]["supervised"])
        self.assertFalse(hello["capabilities"]["grok"]["supervised"])
        self.assertIn("fails open", " ".join(hello["capabilities"]["grok"]["limits"]))
        self.assertEqual(
            hello["notifications"], ["event", "delivery", "output", "attention", "heartbeat"]
        )
        self.assertIn("subscribe", hello["methods"])
        self.assertNotIn("do", hello["commands"])

    def test_only_whitelisted_verbs_run(self):
        for argv, code in (
            (["do", "force-push"], "authorization-argv"),
            (["quiet", "status"], "authorization-argv"),
            (["supervise"], "command-refused"),
            (["focus", "will"], "command-refused"),
            (["dashboard"], "command-refused"),
            (["dashboard", "--once", "--watch"], "command-refused"),
            ([], "supervisor-params"),
            ("status", "supervisor-params"),
            (["status", 1], "supervisor-params"),
        ):
            with self.subTest(argv=argv):
                error = self.call("command", {"argv": argv})["error"]
                self.assertEqual(error["data"], {"code": code})

    def test_a_verb_runs_as_the_captain_of_this_session(self):
        os.environ.update(CAPTAIN_ROLE="crew", CAPTAIN_CREW="will")
        result = self.call("command", {"argv": ["memory", "path", "--scope", "session"]})["result"]
        self.assertEqual((result["exit"], result["stderr"]), (0, ""))
        self.assertEqual(Path(result["stdout"].strip()), self.current.directory)
        self.assertNotIn(self.ready["token"], json.dumps(result))
        status = self.call("command", {"argv": ["status"]})["result"]
        self.assertEqual(status["exit"], 0, status["stderr"])

    def test_a_refusing_verb_returns_its_exit_and_stderr(self):
        result = self.call("command", {"argv": ["tell", "nobody", "hi"]})["result"]
        self.assertNotEqual(result["exit"], 0)
        self.assertIn("No crew named 'nobody'", result["stderr"])

    def test_a_verb_past_its_deadline_is_killed_and_refused(self):
        with patch.dict(supervisor.COMMANDS, {"status": 0.01}):
            error = self.call("command", {"argv": ["status"]})["error"]
        self.assertEqual(error["data"], {"code": "command-timeout"})


if __name__ == "__main__":
    unittest.main()
