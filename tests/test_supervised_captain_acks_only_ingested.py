"""The captain's delivery bridge acknowledges a crew delivery only after the captain's adapter
confirms it landed, and never resends one an earlier supervisor may already have sent."""

import contextlib
import json
import os
import shutil
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
    captain_bridge,
    config,
    protocol,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


def delivery(delivery_id, status="done", summary="Report."):
    return {
        "status": status,
        "delivery_id": delivery_id,
        "crew": "will",
        "assignment_id": "a1",
        "summary": summary,
    }


class BridgeStepTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.directory = supervisor.open_session(self.project, None).directory
        self.rings, self.acks, self.reports, self.mine = [], [], [], set()
        self.landing = True

    def step(self, deliveries):
        def ring(message_id, text):
            self.rings.append((message_id, text))
            return {"landed": self.landing, "gate": None if self.landing else "agent not idle"}

        return captain_bridge.step(
            self.directory,
            deliveries,
            ring,
            lambda item: self.acks.append(item["delivery_id"]),
            self.mine,
            lambda code, message: self.reports.append(code),
        )

    def test_a_held_delivery_is_retried_and_acknowledged_only_once_it_lands(self):
        self.landing = False
        self.assertFalse(self.step([delivery("d1")]))
        self.assertEqual(self.acks, [])
        self.landing = True
        self.assertTrue(self.step([delivery("d1")]))
        self.assertEqual([ring[0] for ring in self.rings], ["d1", "d1"])
        self.assertEqual(self.acks, ["d1"])
        self.assertIn("Captain wait for will (done):\nReport.", self.rings[-1][1])

    def test_busy_refusal_is_retried_after_restart(self):
        self.landing = False
        self.step([delivery("busy")])
        self.mine.clear()
        self.landing = True
        self.step([delivery("busy")])
        self.assertEqual(self.acks, ["busy"])
        self.assertEqual(self.reports, [])

    def test_uncertain_send_is_not_retried_even_before_restart(self):
        for result in ({"landed": False, "gate": "delivery unconfirmed"}, OSError("lost")):
            with self.subTest(result=result):
                item = delivery(str(result))

                def ring(*args):
                    if isinstance(result, Exception):
                        raise result
                    return result

                try:
                    captain_bridge.step(
                        self.directory,
                        [item],
                        ring,
                        self.acks.append,
                        self.mine,
                        lambda *args: None,
                    )
                except OSError:
                    pass
                self.step([item])
                self.assertEqual(self.rings, [])
                self.assertEqual(self.acks, [])

    def test_malformed_claim_can_be_explicitly_resent_or_acknowledged(self):
        for decision in ("resend", "ack"):
            with self.subTest(decision=decision):
                item = delivery(decision)
                captain_bridge.claim(self.directory, item, self.mine)
                captain_bridge._claim_path(self.directory, decision).write_text("{")
                self.mine.clear()
                self.step([item])
                self.assertNotIn(decision, self.acks)
                captain_bridge.resolve_claim(self.directory, decision, decision)
                self.step([item])
                self.assertIn(decision, self.acks)
        self.assertEqual([item[0] for item in self.rings], ["resend"])

    def test_quiet_statuses_are_acknowledged_without_a_captain_turn(self):
        self.step([delivery("d1", "idle"), delivery("d2", "working")])
        self.assertEqual((self.rings, self.acks), ([], ["d1", "d2"]))

    def test_a_delivery_an_earlier_supervisor_claimed_is_never_resent_or_acknowledged(self):
        self.step([delivery("d1")])  # this process claimed, sent and acknowledged it
        self.mine.clear()  # a restarted supervisor
        self.assertTrue(self.step([delivery("d1")]))
        self.assertEqual(self.acks, ["d1", "d1"])  # delivered before: acknowledged, not resent
        captain_bridge.claim(self.directory, delivery("d2"), self.mine)
        self.rings.append(("d2", "unconfirmed send"))
        self.mine.clear()
        self.landing = True
        self.step([delivery("d2")])
        self.assertEqual([ring[0] for ring in self.rings], ["d1", "d2"])
        self.assertNotIn("d2", self.acks)
        self.assertEqual(self.reports, ["captain-delivery-uncertain"])

    def test_notes_ride_the_delivery_and_are_spent_only_when_it_lands(self):
        captain_bridge.add_note(self.directory, ["tell", "will", "go"])
        self.landing = False
        self.step([delivery("d1")])
        self.assertEqual(len(captain_bridge.pending_notes(self.directory)), 1)
        self.landing = True
        self.step([delivery("d1")])
        self.assertIn("The user ran: captain tell will go", self.rings[-1][1])
        self.assertEqual(captain_bridge.pending_notes(self.directory), [])

    def test_a_long_summary_is_cut_and_points_at_the_full_delivery(self):
        self.step([delivery("d1", summary="x" * 5000)])
        text = self.rings[0][1]
        self.assertIn("[Output truncated]", text)
        self.assertLess(len(text), captain_bridge.SUMMARY_CHARS + 600)
        full = text.split("Full delivery: ", 1)[1].splitlines()[0]
        self.assertEqual(json.loads(Path(full).read_text())["delivery"]["summary"], "x" * 5000)


class SupervisedBridgeTests(SessionCase):
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
        self.enterContext(patch.object(supervisor, "BRIDGE_POLL", 0.05))
        self.busy, self.rung = True, []

        def ring(link, message_id, text):
            if self.busy:
                return {"landed": False, "gate": "agent not idle"}
            self.rung.append(message_id)
            return {"landed": True, "gate": None}

        adapter = types.SimpleNamespace(
            open=lambda link: None,
            state=lambda link: "idle",
            ring=ring,
            interrupt=lambda link, reason: "idle",
            set_model=lambda link, model: {"model": model, "applies": "now"},
        )
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": adapter}))
        self.enterContext(
            patch.object(
                supervisor,
                "captain_command",
                side_effect=lambda *args: (NATIVE, dict(os.environ), None, "rpc"),
            )
        )
        self.current = supervisor.open_session(self.project, None)
        record = {"id": "will", "agent": "c-will", "incarnation_id": "b" * 32}
        record["execution"] = "supervised"
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        self.current = self.current._replace(meta=meta)
        crew = Crew("will", record, self.current)
        self.assignment = protocol.begin(crew, self.project, "Map the repo.")["id"]
        protocol.once(crew, "hooks_untrusted", "[codex-hooks-untrusted] Trust them once.")
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

    def acked(self):
        state = store.read_json(self.current.directory / "protocol.json")
        return state["assignments"][self.assignment]["ack_seq"]

    def test_the_captain_bridge_acks_a_crew_delivery_only_after_the_captain_takes_it(self):
        supervision.request(self.current.directory, "start_captain", {"provider": "fake"})
        time.sleep(0.5)  # several bridge rounds against a busy captain
        self.assertEqual((self.rung, self.acked()), ([], 0))
        self.busy = False
        deadline = time.monotonic() + 10
        while self.acked() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.acked(), 1)
        self.assertEqual(len(self.rung), 1)


if __name__ == "__main__":
    unittest.main()
