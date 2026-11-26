"""waitingOn: a supervised crew's question waits on the captain until it is escalated or the
captain ends a turn after receiving it; then it waits on the human, recorded in captain state."""

import contextlib
import json
import os
import threading
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import eventlog, protocol, sessions, store, supervision, supervisor
from captain_barbossa.crew import Crew

INCARNATION = "b" * 32


class WaitingOnTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        record = {
            "id": "will",
            "agent": "captain-will",
            "incarnation_id": INCARNATION,
            "provider": "codex",
            "execution": "supervised",
        }
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        self.current = self.current._replace(meta=meta)
        self.crew = Crew("will", record, self.current)
        self.owner = supervisor.Supervisor(self.current, "token", threading.Event())
        self.assignment = protocol.begin(self.crew, self.project, "Map the repo.")["id"]
        asked = protocol.change(
            self.crew,
            types.SimpleNamespace(
                command="ask", assignment=self.assignment, incarnation=None, question="Which?"
            ),
            self.project,
        )
        self.question = asked["question_id"]

    def captain_turn_ends(self):
        stamp = datetime.now(timezone.utc).isoformat()
        line = {"hook_event_name": "Stop", "timestamp": stamp}
        path = self.current.events("captain")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")

    def routed(self):
        return {
            row["question"]: row["waiting_on"]
            for row in supervisor.questions(self.owner)["questions"]
        }

    def recorded(self):
        state = store.read_json(self.current.directory / "protocol.json")
        return state["assignments"][self.assignment]["question"]

    def test_a_question_waits_on_the_captain_until_it_idles_after_receiving_it(self):
        self.captain_turn_ends()  # a turn that ended before delivery proves nothing
        self.assertEqual(self.routed(), {self.question: "captain"})
        delivery = protocol.poll(self.crew)
        self.assertEqual(delivery["status"], "asked")
        protocol.poll(self.crew, ack=delivery["delivery_id"])
        self.assertIsNotNone(self.recorded()["delivered_at"])
        self.assertEqual(self.routed(), {self.question: "captain"})
        self.captain_turn_ends()
        self.assertEqual(self.routed(), {self.question: "human"})
        self.assertEqual(self.recorded()["waiting_on"], "human")
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        routes = [a["waiting_on"] for a in actions if a.get("state") == "routed"]
        self.assertEqual(routes, ["captain", "human"])

    def test_an_escalated_question_waits_on_the_human_and_stays_there(self):
        with protocol.checkpoint(self.current.directory) as state:
            state["assignments"][self.assignment]["question"]["escalated"] = True
        self.assertEqual(self.routed(), {self.question: "human"})
        with protocol.checkpoint(self.current.directory) as state:
            state["assignments"][self.assignment]["question"]["escalated"] = False
        self.assertEqual(self.routed(), {self.question: "human"})

    def test_waiting_on_is_decided_from_delivery_and_turn_state(self):
        for question, idle, expected in (
            ({}, 10.0, "captain"),
            ({"delivered_at": 5.0}, None, "captain"),
            ({"delivered_at": 5.0}, 4.0, "captain"),
            ({"delivered_at": 5.0}, 6.0, "human"),
            ({"escalated": True}, None, "human"),
            ({"waiting_on": "human"}, None, "human"),
        ):
            with self.subTest(question=question, idle=idle):
                self.assertEqual(protocol.waiting_on(question, idle), expected)


if __name__ == "__main__":
    unittest.main()
