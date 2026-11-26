"""Secrets never leave the supervisor in clear: secret-named keys (apiKey and kin) and the
supervisor token are redacted in notifications, command replies and the protocol action log."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    config,
    eventlog,
    instructions,
    jsonrpc,
    lifetime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew

INCARNATION = "b" * 32
# A stdio peer that answers every request, first emitting a notification carrying a key.
LEAKY_PEER = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    if message['method'] == 'emit':\n"
    "        params = {'delta': 'hi', 'auth': {'apiKey': 'sk-live-output'}}\n"
    "        print(json.dumps({'method': 'account/updated', 'params': params}), flush=True)\n"
    "    print(json.dumps({'id': message['id'], 'result': {'thread': 't-1'}}), flush=True)\n"
)


def open_peer(link):
    link.peer = jsonrpc.Peer(link.stdout, link.stdin, lambda message: None, link.crew_id)
    link.stdin = link.stdout = None
    link.peer.start()
    link.thread = link.peer.request("initialize", {}, 5)["thread"]


def ring_emit(link, message_id, text):
    link.peer.request("emit", {}, 5)
    return {"landed": True, "gate": None}


LEAKY = types.SimpleNamespace(
    open=open_peer,
    state=lambda link: "idle",
    ring=ring_emit,
    interrupt=lambda link, reason: "idle",
    set_model=lambda link, model: {"model": model, "applies": "now"},
)


class SupervisorRedactsSecretsTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=0.2\nshutdown_timeout=2.0\n"
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
        self.enterContext(patch.object(supervisor, "STREAM_INTERVAL", 0.05))
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
        os.environ[supervision.TOKEN_ENV] = self.ready["token"]

    def connect(self, method, params):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(30)
        client.connect(self.ready["socket"])
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"token": self.ready["token"], **params},
        }
        client.sendall(json.dumps(request).encode() + b"\n")
        reader = jsonrpc.LineReader(client.fileno())
        return reader, reader.read(time.monotonic() + 30)

    @staticmethod
    def until(reader, done):
        seen, deadline = [], time.monotonic() + 10
        while not done(seen):
            seen.append(reader.read(deadline))
        return seen

    def test_secret_named_keys_and_literal_secrets_are_redacted(self):
        value = {
            "apiKey": "sk-1",
            "nested": [
                {"api_key": "sk-2", "OPENAI-API-KEY": "sk-3", "client_secret": "sk-4"},
                {"Authorization": "Bearer sk-5", "access_token": "sk-6", "password": "sk-7"},
            ],
            "input_tokens": 12,
            "text": "the token is T0K",
        }
        self.assertEqual(
            eventlog.redact(value, ("T0K",)),
            {
                "apiKey": "[redacted]",
                "nested": [
                    {
                        "api_key": "[redacted]",
                        "OPENAI-API-KEY": "[redacted]",
                        "client_secret": "[redacted]",
                    },
                    {
                        "Authorization": "[redacted]",
                        "access_token": "[redacted]",
                        "password": "[redacted]",
                    },
                ],
                "input_tokens": 12,
                "text": "the token is [redacted]",
            },
        )

    def test_output_notifications_never_carry_an_api_key(self):
        record = {
            "id": "will",
            "agent": "captain-will",
            "incarnation_id": INCARNATION,
            "provider": "leaky",
            "execution": "supervised",
        }
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        self.current = self.current._replace(meta=meta)
        ref = lifetime.prepare(
            self.current, record, [sys.executable, "-c", LEAKY_PEER], {}, self.project
        )
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"]["lifetime_id"] = ref
        crew = Crew("will", meta["crew"]["will"], self.current._replace(meta=meta))
        with patch.dict(supervisor.ADAPTERS, {"leaky": LEAKY}):
            supervision.start(crew, ref)
            reader, _ = self.connect("subscribe", {"cursors": {}})
            supervision.ring(crew, "m-1", "go")
            seen = self.until(reader, lambda seen: any(n["method"] == "output" for n in seen))
            supervision.stop(crew, "done")
        output = next(n["params"] for n in seen if n["method"] == "output")
        self.assertEqual(output["params"], {"delta": "hi", "auth": {"apiKey": "[redacted]"}})
        self.assertNotIn("sk-live-output", json.dumps(seen))

    def test_event_lines_and_protocol_actions_never_carry_an_api_key(self):
        events = self.current.events(f"will-{INCARNATION}")
        events.parent.mkdir(parents=True, exist_ok=True)
        line = {"hook_event_name": "PreToolUse", "tool_input": {"apiKey": "sk-live-event"}}
        events.write_text(json.dumps(line) + "\n", encoding="utf-8")
        eventlog.record_action(
            self.current.directory,
            "mail",
            "will",
            "a1",
            supervised=True,
            state="sent",
            auth={"apiKey": "sk-live-log"},
        )
        self.assertNotIn(b"sk-live-log", (self.current.directory / eventlog.ACTIONS).read_bytes())
        reader, _ = self.connect("subscribe", {"cursors": {}})
        seen = self.until(
            reader, lambda seen: len([n for n in seen if n["method"] == "event"]) >= 2
        )
        text = json.dumps(seen)
        self.assertNotIn("sk-live-event", text)
        self.assertNotIn("sk-live-log", text)
        self.assertIn('"apiKey": "[redacted]"', text)

    def test_the_action_log_never_holds_the_supervisor_token(self):
        token = self.ready["token"]
        eventlog.record_action(
            self.current.directory, "mail", "will", "a1", supervised=True, text=f"use {token}"
        )
        os.environ.pop(supervision.TOKEN_ENV)  # the supervisor's own writes know it too
        eventlog.record_action(
            self.current.directory, "lifecycle", "will", "a1", supervised=True, detail=token
        )
        log = (self.current.directory / eventlog.ACTIONS).read_text(encoding="utf-8")
        self.assertNotIn(token, log)
        self.assertEqual(log.count(eventlog.REDACTED), 2)

    def script(self, body):
        path = self.root / "fake-captain"
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_command_output_redacts_serialized_secrets_before_clipping(self):
        pad = supervisor.COMMAND_OUTPUT - 5  # the token straddles the cut
        script = self.script(
            'printf \'{"apiKey": "sk-cmd", "tokens": 3}\\n\' >&2; '
            f"head -c {pad} /dev/zero | tr '\\0' x; printf %s \"$CAPTAIN_SUPERVISOR_TOKEN\""
        )
        with patch.object(instructions, "python_command", return_value=str(script)):
            _, reply = self.connect("command", {"argv": ["status"]})
        result = reply["result"]
        self.assertEqual(result["stderr"], '{"apiKey": "[redacted]", "tokens": 3}\n')
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"]), supervisor.COMMAND_OUTPUT)
        self.assertTrue(result["stdout"].endswith("x[reda"))
        self.assertNotIn(self.ready["token"][:5], result["stdout"][-10:])

    def test_command_output_is_read_into_a_bounded_buffer(self):
        script = self.script("head -c 8000000 /dev/zero")
        process = subprocess.Popen(
            [str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        stdout, stderr = supervisor._capture(process, 30)
        self.assertEqual(len(stdout), supervisor.COMMAND_OUTPUT + supervisor.REDACT_MARGIN)
        self.assertEqual((stderr, process.returncode), (b"", 0))

    def test_command_replies_never_echo_the_supervisor_token(self):
        script = self.root / "echo-token"
        script.write_text('#!/bin/sh\necho "token=$CAPTAIN_SUPERVISOR_TOKEN"\n', encoding="utf-8")
        script.chmod(0o700)
        with patch.object(instructions, "python_command", return_value=str(script)):
            _, reply = self.connect("command", {"argv": ["status"]})
        self.assertEqual(reply["result"]["stdout"], "token=[redacted]\n")


if __name__ == "__main__":
    unittest.main()
