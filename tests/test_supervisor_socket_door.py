"""The supervisor socket door: token-gated, bounded, and a dead or broken peer is a CaptainError."""

import contextlib
import json
import os
import shutil
import socket
import stat
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
    cli,
    config,
    instructions,
    jsonrpc,
    lifetime,
    protocol,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError

SOCKET_PATH = supervision.socket_path
# A stdio JSON-RPC peer that answers every request with a fixed thread id.
ECHO_PEER = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    print(json.dumps({'id': message['id'], 'result': {'thread': 't-1'}}), flush=True)\n"
)


def open_fake(link):
    deadline = time.monotonic() + 5
    jsonrpc.write(link.stdin, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, deadline)
    link.thread = jsonrpc.LineReader(link.stdout).read(deadline)["result"]["thread"]


FAKE_ADAPTER = types.SimpleNamespace(
    open=open_fake,
    state=lambda link: "idle",
    ring=lambda link, message_id, text: {"landed": True, "gate": None},
    interrupt=lambda link, reason: "idle",
    set_model=lambda link, model: {"model": model, "applies": "next-turn"},
)


class SupervisorSocketDoorTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.write_settings("")
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

    def write_settings(self, extra):
        settings = self.project / ".captain"
        settings.mkdir(exist_ok=True)
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=0.2\nshutdown_timeout=2.0\n"
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=5.0\n"
            "hook_timeout=5.0\n" + extra,
        )
        config.settings.cache_clear()

    def serve(self, current=None):
        """Run a supervisor thread; return its readiness line and its stop event."""
        read_fd, write_fd = os.pipe()
        stopped, errors = threading.Event(), []

        def target():
            with os.fdopen(write_fd, "w") as out:
                try:
                    supervisor.serve(current or self.current, out, stopped)
                except (CaptainError, OSError) as exc:
                    errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            line = lines.readline()
        if not line:
            thread.join(30)
            raise errors[0]
        ready = json.loads(line)
        os.environ[supervision.TOKEN_ENV] = ready["token"]
        return ready, stopped, thread

    def supervised_crew(
        self, provider="fake", execution="supervised", incarnation="b" * 32, name="will"
    ):
        record = {
            "id": name,
            "incarnation_id": incarnation,
            "provider": provider,
            "execution": execution,
        }
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"][name] = record
        self.current = self.current._replace(meta=meta)
        ref = lifetime.prepare(
            self.current,
            record,
            [sys.executable, "-c", ECHO_PEER],
            {"CAPTAIN_CREW": name},
            self.project,
        )
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"][name]["lifetime_id"] = ref
        record["lifetime_id"] = ref
        self.current = self.current._replace(meta=meta)
        return Crew(name, record, self.current), ref

    def test_ready_line_hands_out_the_token_and_no_file_holds_it(self):
        ready, stopped, _ = self.serve()
        self.assertEqual(ready["session_id"], self.current.meta["id"])
        self.assertEqual(store.read_json(self.current.meta_path)["transport"], "supervisor")
        path = Path(ready["socket"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(supervision.alive(self.current.directory))
        for root in (self.root, self.sockets):
            for item in root.rglob("*"):
                if item.is_file() and not item.is_socket():
                    self.assertNotIn(ready["token"].encode(), item.read_bytes(), item)

    def test_wrong_or_missing_token_is_refused(self):
        self.serve()
        os.environ[supervision.TOKEN_ENV] = "wrong"
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-token\] Wrong"):
            supervision.request(self.current.directory, "state", {"crew": "will"})
        del os.environ[supervision.TOKEN_ENV]
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-token\].*not set"):
            supervision.request(self.current.directory, "state", {"crew": "will"})

    def test_dead_supervisor_is_a_captain_error_at_the_door(self):
        os.environ[supervision.TOKEN_ENV] = "token"
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-gone\]"):
            supervision.request(self.current.directory, "state", {"crew": "will"})
        ready, stopped, thread = self.serve()
        self.assertEqual(
            supervision.request(self.current.directory, "state", {"crew": "will"}),
            {"crew": "will", "state": None},
        )
        stopped.set()
        thread.join(30)
        self.assertFalse(Path(ready["socket"]).exists())
        self.assertFalse(supervision.alive(self.current.directory))
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-gone\]"):
            supervision.request(self.current.directory, "state", {"crew": "will"})

    def test_malformed_or_silent_reply_is_a_captain_error(self):
        os.environ[supervision.TOKEN_ENV] = "token"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(supervision.socket_path(self.current.directory)))
        listener.listen()
        replies = [b"not json\n", b'{"id": 1, "result": 1, "error": {}}\n', None]
        held = []

        def answer():
            for reply in replies:
                connection, _ = listener.accept()
                held.append(connection)
                if reply is not None:
                    connection.sendall(reply)

        threading.Thread(target=answer, daemon=True).start()
        for pattern in (r"\[jsonrpc-malformed\] Not a JSON-RPC line: not json",) + (
            r"\[jsonrpc-malformed\] Not a JSON-RPC message",
        ):
            with self.assertRaisesRegex(CaptainError, pattern):
                supervision.request(self.current.directory, "state", {"crew": "will"})
        with self.assertRaisesRegex(CaptainError, r"\[jsonrpc-timeout\]"):
            supervision.request(self.current.directory, "state", {"crew": "will"}, timeout=0.2)
        for connection in held:
            connection.close()

    def raw(self, ready, line):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(ready["socket"])
            client.sendall(line)
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + 5)

    def test_every_refusal_is_typed_with_its_protocol_code(self):
        ready, _, _ = self.serve()
        token = ready["token"]
        for line, code, wanted in (
            (b"not json\n", supervisor.INVALID_REQUEST, "jsonrpc-malformed"),
            (b'{"id": 1, "method": "state", "params": []}\n', -32600, "jsonrpc-malformed"),
            (b'{"id": 1, "method": "state", "params": {}}\n', -32001, "supervisor-token"),
            (
                json.dumps({"id": 1, "method": "nope", "params": {"token": token}}).encode()
                + b"\n",
                supervisor.UNKNOWN_METHOD,
                "unknown-method",
            ),
            (
                json.dumps({"id": 1, "method": "state", "params": {"token": token}}).encode()
                + b"\n",
                supervisor.REFUSED,
                "supervisor-params",
            ),
        ):
            with self.subTest(protocol=wanted, code=code):
                error = self.raw(ready, line)["error"]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["data"], {"code": wanted})
                self.assertTrue(error["message"].startswith(f"[{wanted}] "))
        self.assertEqual(
            supervisor.refusal("Lifetime launch was revoked.")["message"],
            "[supervisor-error] Lifetime launch was revoked.",
        )

    def test_socket_path_is_refused_in_temp_scratch_and_at_104_bytes(self):
        long_dir = "/" + "x" * 90
        for socket_dir, pattern in (
            (tempfile.gettempdir(), "crew share as scratch"),
            ("/tmp/captain", "crew share as scratch"),
            (long_dir, "104 bytes or more"),
            ("relative/run", "must be absolute"),
        ):
            with self.subTest(socket_dir=socket_dir):
                self.write_settings(f'socket_dir = "{socket_dir}"\n')
                with self.assertRaisesRegex(CaptainError, pattern):
                    SOCKET_PATH(self.current.directory)
        self.write_settings('socket_dir = "/Users/someone/.captain/run"\n')
        path = SOCKET_PATH(self.current.directory)
        self.assertEqual(path.name, f"s-{self.current.meta['id'][:12]}.sock")
        self.assertLess(len(os.fsencode(path)), supervision.SOCKET_PATH_LIMIT)
        self.assertFalse(path.parent.exists())

    def test_one_supervisor_per_session(self):
        _, stopped, thread = self.serve()
        out = tempfile.TemporaryFile("w+")
        self.addCleanup(out.close)
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-running\]"):
            supervisor.serve(self.current, out, threading.Event())
        self.assertTrue(supervision.alive(self.current.directory))
        stopped.set()
        thread.join(30)
        self.assertFalse(supervision.alive(self.current.directory))

    def test_herdr_session_is_refused(self):
        herdr = sessions.session(self.project, self.pane, create=True)
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-herdr-session\]"):
            supervisor.open_session(self.project, herdr.meta["id"])
        self.assertNotIn("transport", store.read_json(herdr.meta_path))

    def test_start_owns_the_crew_lifetime_and_stop_leaves_an_exit_receipt(self):
        crew, ref = self.supervised_crew()
        self.serve()
        with patch.dict(supervisor.ADAPTERS, {"fake": FAKE_ADAPTER}):
            self.assertEqual(supervision.start(crew, ref), {"crew": "will", "state": "started"})
            self.assertEqual(lifetime.read(self.current, ref)["state"], "claimed")
            self.assertEqual(supervision.state(crew), "idle")
            self.assertEqual(
                supervision.set_model(crew, "gpt-6.2-sol"),
                {"crew": "will", "model": "gpt-6.2-sol", "applies": "next-turn"},
            )
            with self.assertRaisesRegex(CaptainError, r"already running"):
                supervision.start(crew, ref)
            self.assertEqual(supervision.stop(crew, "dismissed")["outcome"], "stopped")
        self.assertEqual(lifetime.read(self.current, ref)["state"], "exited")
        self.assertIsNone(supervision.state(crew))
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-crew\].*not running"):
            supervision.ring(crew, "m-1", "hello")

    def test_a_crew_cannot_start_another_crews_lifetime(self):
        crew, _ = self.supervised_crew()
        _, other = self.supervised_crew(incarnation="c" * 32, name="jack")
        self.serve()
        with patch.dict(supervisor.ADAPTERS, {"fake": FAKE_ADAPTER}):
            with self.assertRaisesRegex(CaptainError, r"is not will's own launch"):
                supervision.start(crew, other)
        self.assertEqual(lifetime.read(self.current, other)["state"], "prepared")

    def test_a_crew_that_exits_alone_is_reconciled_while_its_supervisor_lives(self):
        crew, ref = self.supervised_crew()
        assignment = {"state": "working", "notices": []}
        self.serve()
        protocol.reconcile_supervised(crew, assignment)
        self.assertEqual(assignment["state"], "working")  # prepared is still starting
        with patch.dict(supervisor.ADAPTERS, {"fake": FAKE_ADAPTER}):
            supervision.start(crew, ref)
            protocol.reconcile_supervised(crew, assignment)
            self.assertEqual(assignment["state"], "working")  # owned and running
            supervision.stop(crew, "gone")
        self.assertTrue(supervision.alive(self.current.directory))
        protocol.reconcile_supervised(crew, assignment)
        self.assertEqual((assignment["state"], assignment["outcome"]), ("done", "lost"))
        self.assertIn("[supervised-crew-exited]", assignment["notices"][0]["summary"])

    def test_admission_closes_under_the_same_lock_stop_all_reads(self):
        crew, ref = self.supervised_crew()
        owner = supervisor.Supervisor(self.current, "token", threading.Event())
        closing = lambda *_: owner.closing.set()  # noqa: E731 - shutdown lands mid-start
        with (
            patch.dict(supervisor.ADAPTERS, {"fake": FAKE_ADAPTER}),
            patch.object(supervisor, "bound_launch", side_effect=closing),
            self.assertRaisesRegex(CaptainError, r"\[supervisor-closing\]"),
        ):
            supervisor.start(owner, {"crew": "will", "lifetime_ref": ref})
        self.assertEqual(owner.links, {})
        self.assertEqual(lifetime.read(self.current, ref)["state"], "prepared")

    def test_a_peer_that_stops_reading_costs_a_write_its_deadline_not_forever(self):
        stalled_read, stalled_write = os.pipe()
        silent_read, silent_write = os.pipe()
        self.addCleanup(os.close, stalled_read)
        self.addCleanup(os.close, silent_write)
        peer = jsonrpc.Peer(silent_read, stalled_write, lambda message: None)
        peer.start()
        errors = []

        def blocked_request():
            try:
                peer.request("turn/start", {"text": "x" * (1 << 20)}, 0.3)
            except CaptainError as exc:
                errors.append(str(exc))
            peer.close()

        thread = threading.Thread(target=blocked_request, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIn("[jsonrpc-timeout]", errors[0])
        self.assertIsNone(peer.write_fd)

    def test_a_client_leaving_keeps_crew_and_shutdown_stops_them_all(self):
        crew, ref = self.supervised_crew()
        ready, _, thread = self.serve()
        with patch.dict(supervisor.ADAPTERS, {"fake": FAKE_ADAPTER}):
            supervision.start(crew, ref)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(ready["socket"])
            self.assertEqual(supervision.state(crew), "idle")
            outcomes = supervision.shutdown(self.current.directory)
        self.assertEqual([(row["crew"], row["outcome"]) for row in outcomes], [("will", "stopped")])
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertFalse(Path(ready["socket"]).exists())
        self.assertEqual(lifetime.read(self.current, ref)["state"], "exited")

    def test_crew_without_a_supervised_transport_is_refused(self):
        self.serve()
        for provider, execution, incarnation, pattern in (
            ("fake", None, "b" * 32, "not a supervised crew"),
            ("grok", "supervised", "c" * 32, r"\[supervisor-transport\] grok"),
        ):
            with self.subTest(provider=provider, execution=execution):
                crew, ref = self.supervised_crew(provider, execution, incarnation)
                with self.assertRaisesRegex(CaptainError, pattern):
                    supervision.start(crew, ref)
                self.assertEqual(lifetime.read(self.current, ref)["state"], "prepared")

    def test_herdr_commands_and_hooks_never_load_the_supervisor(self):
        self.assertEqual(cli.SUPERVISOR_TOKEN_ENV, supervision.TOKEN_ENV)
        code = (
            "import sys; import captain_barbossa.cli, captain_barbossa.events; "
            "print(sorted(m for m in sys.modules if m.rsplit('.', 1)[-1] in "
            "('supervision', 'supervisor', 'jsonrpc', 'codex_app_server')))"
        )
        loaded = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
        )
        self.assertEqual(loaded.stdout.strip(), "[]")

    def test_no_child_of_the_supervisor_inherits_its_token(self):
        link = supervisor.Link("will", "b" * 32, FAKE_ADAPTER, self.root / "events.jsonl")
        os.environ[supervision.TOKEN_ENV] = "inherited"
        echo = "import json, os; print(json.dumps({'token': os.environ.get(%r)}))"
        echo %= supervision.TOKEN_ENV
        with patch.object(
            instructions, "hook_command", side_effect=lambda events: [sys.executable, "-c", echo]
        ):
            self.assertEqual(supervisor.relay_hook(link, {})[1], {"token": None})
        fresh = sessions.session(self.project, None, create=True)
        seen = []
        with patch.object(
            supervisor,
            "serve",
            side_effect=lambda *_: seen.append(os.environ.get(supervision.TOKEN_ENV)),
        ):
            supervisor.run(types.SimpleNamespace(session=fresh.meta["id"]), self.project)
        self.assertEqual(seen, [None])

    def test_relayed_hook_gets_the_payload_and_returns_its_output(self):
        link = supervisor.Link("will", "b" * 32, FAKE_ADAPTER, self.root / "events.jsonl")
        link.env = {"CAPTAIN_CREW": "will"}
        echo = (
            "import json, os, sys; payload = json.load(sys.stdin); "
            "print(json.dumps({'payload': payload, 'crew': os.environ['CAPTAIN_CREW'], "
            "'events': sys.argv[1]}))"
        )
        with patch.object(
            instructions,
            "hook_command",
            side_effect=lambda events: [sys.executable, "-c", echo, str(events)],
        ):
            code, output = supervisor.relay_hook(link, {"hook_event_name": "PermissionRequest"})
        self.assertEqual(code, 0)
        self.assertEqual(
            output,
            {
                "payload": {"hook_event_name": "PermissionRequest"},
                "crew": "will",
                "events": str(self.root / "events.jsonl"),
            },
        )

    def test_supervise_command_marks_the_session_and_serves_it(self):
        fresh = sessions.session(self.project, None, create=True)
        with patch.object(supervisor, "serve") as serve:
            self.assertEqual(cli.main(["--session", fresh.meta["id"], "supervise"]), 0)
        current = serve.call_args.args[0]
        self.assertEqual(current.meta["transport"], "supervisor")
        self.assertEqual(store.read_json(fresh.meta_path)["transport"], "supervisor")

    def test_only_a_token_holder_takes_the_supervised_paneless_path(self):
        args = cli.parser().parse_args(["--session", self.current.meta["id"], "tell", "will", "hi"])
        self.assertFalse(cli.paneless_target(args))
        os.environ[supervision.TOKEN_ENV] = "token"
        self.assertTrue(cli.paneless_target(args))


if __name__ == "__main__":
    unittest.main()
