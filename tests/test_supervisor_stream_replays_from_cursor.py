"""subscribe: event files and protocol actions replay from byte cursors, deliveries are a
never-acknowledged projection, and output and attention are live only."""

import contextlib
import json
import os
import queue
import shutil
import socket
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

INCARNATION = "b" * 32
# A stdio peer that answers every request, first emitting one notification for "emit".
EMIT_PEER = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    if message['method'] == 'emit':\n"
    "        note = {'method': 'item/agentMessage/delta', 'params': {'delta': 'hi'}}\n"
    "        print(json.dumps(note), flush=True)\n"
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


def refuse(link):
    raise CaptainError("[codex-hooks-untrusted] Codex has not trusted captain's hooks.")


EMITTER = types.SimpleNamespace(
    open=open_peer,
    state=lambda link: "idle",
    ring=ring_emit,
    interrupt=lambda link, reason: "idle",
    set_model=lambda link, model: {"model": model, "applies": "now"},
)
UNTRUSTED = types.SimpleNamespace(**{**vars(EMITTER), "open": refuse})


class SupervisorStreamReplayTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=0.2\nshutdown_timeout=2.0\n"
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=5.0\n"
            "hook_timeout=5.0\n",
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
        self.enterContext(patch.object(supervisor, "DELIVERY_INTERVAL", 0.1))
        self.enterContext(patch.object(supervisor, "STREAM_INTERVAL", 0.05))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)

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
        with os.fdopen(read_fd) as lines:
            ready = json.loads(lines.readline())
        os.environ[supervision.TOKEN_ENV] = ready["token"]
        return ready

    def crew(self, name="will", incarnation=INCARNATION, provider="fake", argv=None):
        record = {
            "id": name,
            "agent": f"captain-{name}",
            "incarnation_id": incarnation,
            "provider": provider,
            "execution": "supervised",
        }
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"][name] = record
        self.current = self.current._replace(meta=meta)
        if argv is not None:
            ref = lifetime.prepare(self.current, record, argv, {"CAPTAIN_CREW": name}, self.project)
            with sessions.crew_meta(self.current.directory) as meta:
                meta["crew"][name]["lifetime_id"] = ref
            self.current = self.current._replace(meta=meta)
            record["lifetime_id"] = ref
        return Crew(name, record, self.current)

    def subscribe(self, ready, cursors):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(5)
        client.connect(ready["socket"])
        params = {"token": ready["token"], "cursors": cursors}
        request = {"jsonrpc": "2.0", "id": 1, "method": "subscribe", "params": params}
        client.sendall(json.dumps(request).encode() + b"\n")
        reader = jsonrpc.LineReader(client.fileno())
        return client, reader, reader.read(time.monotonic() + 5)

    def until(self, reader, done, timeout=10):
        seen, deadline = [], time.monotonic() + timeout
        while not done(seen):
            seen.append(reader.read(deadline))
        return seen

    def quiet(self, reader, seconds):
        """Everything that arrives within `seconds`."""
        seen, deadline = [], time.monotonic() + seconds
        with contextlib.suppress(CaptainError):
            while True:
                seen.append(reader.read(deadline))
        return seen

    @staticmethod
    def batches(seen, stream):
        return [
            n["params"] for n in seen if n["method"] == "event" and n["params"]["stream"] == stream
        ]

    @staticmethod
    def append(path, event):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")

    def test_events_and_actions_replay_from_a_cursor_with_stable_ids(self):
        self.crew()
        stem = f"will-{INCARNATION}"
        events = self.current.events(stem)
        self.append(events, {"hook_event_name": "SessionStart", "seq": 1})
        first = eventlog.record_action(
            self.current.directory, "mail", "will", "a1", supervised=True, message="m1"
        )
        ready = self.serve()
        client, reader, reply = self.subscribe(ready, {})
        self.assertEqual(reply["result"]["session_id"], self.current.meta["id"])
        ends = reply["result"]["ends"]
        seen = self.until(
            reader, lambda seen: self.batches(seen, "actions") and self.batches(seen, stem)
        )
        (actions,), (native,) = self.batches(seen, "actions"), self.batches(seen, stem)
        for name, batch in (("actions", actions), (stem, native)):
            held = {"offset": batch["cursor"], "generation": batch["generation"]}
            self.assertEqual(ends[name], held)
        self.assertEqual([action["id"] for action in actions["events"]], [first])
        self.assertIsNone(actions["crew"])
        self.assertEqual(native["crew"], "will")
        self.assertEqual(native["events"], [{"hook_event_name": "SessionStart", "seq": 1}])
        self.assertEqual(native["cursor"], events.stat().st_size)
        client.close()

        self.append(events, {"hook_event_name": "Stop", "seq": 2})
        second = eventlog.record_action(
            self.current.directory, "report", "will", "a1", supervised=True, report="Mapped."
        )
        cursors = {
            "actions": {"offset": actions["cursor"], "generation": actions["generation"]},
            stem: {"offset": native["cursor"], "generation": native["generation"]},
        }
        _, reader, _ = self.subscribe(ready, cursors)
        seen = self.until(
            reader, lambda seen: self.batches(seen, "actions") and self.batches(seen, stem)
        )
        self.assertEqual([a["id"] for a in self.batches(seen, "actions")[0]["events"]], [second])
        self.assertEqual(self.batches(seen, stem)[0]["events"][0]["hook_event_name"], "Stop")

        _, reader, _ = self.subscribe(ready, {stem: native["cursor"]})
        seen = self.until(reader, lambda seen: self.batches(seen, "actions"))
        replayed = self.batches(seen, "actions")[0]["events"]
        self.assertEqual([action["id"] for action in replayed], [first, second])

    def test_subscribe_returns_each_streams_end_short_of_a_partial_line(self):
        self.crew()
        stem = f"will-{INCARNATION}"
        events = self.current.events(stem)
        self.append(events, {"seq": 1})
        whole = events.stat().st_size
        with events.open("a", encoding="utf-8") as file:
            file.write('{"seq": 2')
        ready = self.serve()
        _, _, reply = self.subscribe(ready, {stem: {"offset": whole, "generation": None}})
        self.assertEqual(reply["result"]["ends"][stem]["offset"], whole)

    def test_a_quiet_subscription_gets_heartbeats(self):
        self.enterContext(patch.object(supervisor, "HEARTBEAT_INTERVAL", 0.2))
        ready = self.serve()
        _, reader, _ = self.subscribe(ready, {})
        beats = [n for n in self.quiet(reader, 1.0) if n["method"] == "heartbeat"]
        self.assertGreaterEqual(len(beats), 2)
        self.assertIsInstance(beats[0]["params"]["at"], float)

    def test_cursors_must_name_streams_and_offsets(self):
        ready = self.serve()
        for cursors in ({"../events/x": 0}, {"actions": -1}, {"actions": "0"}, ["actions"]):
            with self.subTest(cursors=cursors):
                _, _, reply = self.subscribe(ready, cursors)
                self.assertEqual(reply["error"]["data"], {"code": "supervisor-params"})

    def test_deliveries_are_projected_once_and_never_acknowledged(self):
        crew = self.crew()
        assignment = protocol.begin(crew, self.project, "Map the repo.")
        protocol.once(crew, "hooks_untrusted", "[codex-hooks-untrusted] Trust them once.")
        ready = self.serve()
        _, reader, _ = self.subscribe(ready, {})
        seen = self.until(reader, lambda seen: any(n["method"] == "delivery" for n in seen))
        seen += self.quiet(reader, 0.6)
        delivered = [n["params"] for n in seen if n["method"] == "delivery"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["status"], "error")
        self.assertIn("[codex-hooks-untrusted]", delivered[0]["summary"])
        recorded = store.read_json(self.current.directory / "protocol.json")
        self.assertEqual(recorded["assignments"][assignment["id"]]["ack_seq"], 0)
        waited = protocol.wait(crew, 0)
        self.assertEqual(waited["delivery_id"], delivered[0]["delivery_id"])

    def test_output_and_attention_are_live_and_never_replayed(self):
        will = self.crew(argv=[sys.executable, "-c", EMIT_PEER])
        jack = self.crew("jack", "c" * 32, "untrusted", [sys.executable, "-c", EMIT_PEER])
        ready = self.serve()
        adapters = {"fake": EMITTER, "untrusted": UNTRUSTED}
        with patch.dict(supervisor.ADAPTERS, adapters):
            supervision.start(will, will.record["lifetime_id"])
            client, reader, _ = self.subscribe(ready, {})
            supervision.ring(will, "m-1", "go")
            with self.assertRaisesRegex(CaptainError, r"\[codex-hooks-untrusted\]"):
                supervision.start(jack, jack.record["lifetime_id"])
            seen = self.until(
                reader,
                lambda seen: {"output", "attention"} <= {n["method"] for n in seen},
            )
            output = next(n["params"] for n in seen if n["method"] == "output")
            self.assertEqual(
                output,
                {"crew": "will", "method": "item/agentMessage/delta", "params": {"delta": "hi"}},
            )
            alert = next(n["params"] for n in seen if n["method"] == "attention")
            self.assertEqual((alert["crew"], alert["code"]), ("jack", "codex-hooks-untrusted"))
            client.close()

            _, reader, _ = self.subscribe(ready, {})
            seen = self.quiet(reader, 1.0)
            self.assertEqual({n["method"] for n in seen} - {"event", "delivery"}, set())
            lifecycle = [
                (action["crew"], action["state"])
                for batch in self.batches(seen, "actions")
                for action in batch["events"]
                if action["kind"] == "lifecycle"
            ]
            self.assertEqual(lifecycle, [("will", "started"), ("jack", "failed")])
            supervision.stop(will, "Crew dismissed.")
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        self.assertEqual(
            (actions[-1]["state"], actions[-1]["detail"]), ("stopped", "Crew dismissed.")
        )

    def test_protocol_actions_are_recorded_with_ids_only_in_supervised_sessions(self):
        crew = self.crew()
        assignment = protocol.begin(crew, self.project, "Map the repo.")
        aid = assignment["id"]
        sent = protocol.deliver(
            crew, "Map the repo.", assignment_id=aid, initial=True, queue_only=True
        )
        protocol.receipt(self.current.directory, "will", [sent])
        protocol.grant(crew, aid, "git fetch")
        asked = protocol.change(
            crew,
            types.SimpleNamespace(
                command="ask", assignment=aid, incarnation=None, question="May I push?"
            ),
            self.project,
        )
        answer = protocol.deliver(
            crew, "No.", assignment_id=aid, question_id=asked["question_id"], queue_only=True
        )
        protocol.receipt(self.current.directory, "will", [answer])
        protocol.change(
            crew,
            types.SimpleNamespace(command="done", assignment=aid, incarnation=None, report="Done."),
            self.project,
        )
        # A report is filed under the protocol lock, so its line waits in the spool for a flush.
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        self.assertEqual(
            [(action["kind"], action.get("state")) for action in actions],
            [
                ("recruit", None),
                ("mail", "sent"),
                ("mail", "read"),
                ("grant", "granted"),
                ("question", "asked"),
                ("mail", "sent"),
                ("mail", "read"),
                ("report", None),
            ],
        )
        self.assertEqual(len({action["id"] for action in actions}), len(actions))
        self.assertEqual(
            {(action["crew"], action["assignment"]) for action in actions}, {("will", aid)}
        )
        self.assertEqual(actions[5]["question"], asked["question_id"])
        self.assertEqual(actions[-1]["report"], "Done.")

        herdr = sessions.session(self.project, self.pane, create=True)
        with sessions.crew_meta(herdr.directory) as meta:
            meta["crew"]["will"] = {**crew.record, "execution": None}
        herdr_crew = Crew("will", meta["crew"]["will"], herdr._replace(meta=meta))
        other = protocol.begin(herdr_crew, self.project, "Map the docs.")
        message = protocol.deliver(
            herdr_crew, "Map the docs.", assignment_id=other["id"], queue_only=True
        )
        reads = []
        real = store.read_json

        def spy(path, **options):
            reads.append(Path(path).name)
            return real(path, **options)

        with (
            patch.object(protocol, "read_json", side_effect=spy),
            patch.object(eventlog, "read_json", side_effect=spy),
        ):
            protocol.receipt(herdr.directory, "will", [message])
        self.assertNotIn("session.json", reads)
        self.assertFalse((herdr.directory / eventlog.ACTIONS).exists())
        self.assertFalse((herdr.directory / eventlog.ACTION_SPOOL).exists())

    def test_a_busy_or_broken_action_log_never_blocks_or_breaks_mail(self):
        crew = self.crew()
        aid = protocol.begin(crew, self.project, "Map the repo.")["id"]
        log = self.current.directory / eventlog.ACTIONS
        held = store.lock(self.current.directory / "actions.lock")
        held.__enter__()
        started = time.monotonic()
        sent = protocol.deliver(crew, "First.", assignment_id=aid, queue_only=True)
        self.assertLess(time.monotonic() - started, eventlog.ACTION_LOCK_WAIT + 2)
        held.__exit__(None, None, None)
        log.unlink()
        log.mkdir()  # appends now fail outright
        protocol.receipt(self.current.directory, "will", [sent])
        mail = store.read_json(protocol.mail_dir(self.current.directory, "will") / f"{sent}.json")
        self.assertEqual(mail["state"], "read")
        log.rmdir()
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(log, 0)
        self.assertEqual(
            [(action["kind"], action.get("state")) for action in actions],
            [("mail", "sent"), ("mail", "read")],
        )
        self.assertEqual(list((self.current.directory / eventlog.ACTION_SPOOL).iterdir()), [])

    def test_a_replaced_or_truncated_stream_replays_from_its_start_with_reset(self):
        self.crew()
        stem = f"will-{INCARNATION}"
        events = self.current.events(stem)
        self.append(events, {"hook_event_name": "SessionStart", "seq": 1})
        self.append(events, {"hook_event_name": "Stop", "seq": 2})
        ready = self.serve()
        client, reader, _ = self.subscribe(ready, {})
        first = self.until(reader, lambda seen: self.batches(seen, stem))
        held = self.batches(first, stem)[0]
        self.assertFalse(held["reset"])
        client.close()
        cursor = {stem: {"offset": held["cursor"], "generation": held["generation"]}}

        events.unlink()
        self.append(events, {"hook_event_name": "SessionStart", "seq": 1, "replaced": True})
        client, reader, _ = self.subscribe(ready, cursor)
        replaced = self.batches(self.until(reader, lambda seen: self.batches(seen, stem)), stem)
        self.assertTrue(replaced[0]["reset"])
        self.assertEqual(replaced[0]["events"][0]["replaced"], True)
        client.close()

        events.write_text("", encoding="utf-8")  # truncated in place: same file, shorter
        self.append(events, {"hook_event_name": "Stop", "seq": 3})
        _, reader, _ = self.subscribe(ready, {stem: held["cursor"]})
        truncated = self.batches(self.until(reader, lambda seen: self.batches(seen, stem)), stem)
        self.assertTrue(truncated[0]["reset"])
        self.assertEqual(truncated[0]["events"], [{"hook_event_name": "Stop", "seq": 3}])

    def test_a_delivery_that_leaves_the_projection_is_forgotten(self):
        first = {"status": "idle", "delivery_id": "d-1", "crew": "will", "summary": ""}
        calls = []

        def projection(supervisor_):
            calls.append(None)
            return [] if len(calls) == 2 else [first]

        self.enterContext(patch.object(supervisor, "deliveries", side_effect=projection))
        ready = self.serve()
        _, reader, _ = self.subscribe(ready, {})
        seen = self.until(
            reader, lambda seen: len([n for n in seen if n["method"] == "delivery"]) == 2
        )
        self.assertEqual([n["params"] for n in seen if n["method"] == "delivery"], [first] * 2)

    def test_a_full_feed_drops_its_oldest_output(self):
        owner = supervisor.Supervisor(self.current, "token", threading.Event())
        feed = queue.Queue(2)
        owner.feeds.append(feed)
        for number in range(5):
            supervisor.publish(owner, "output", {"n": number})
        self.assertEqual([feed.get_nowait()[1]["n"] for _ in range(2)], [3, 4])


if __name__ == "__main__":
    unittest.main()
