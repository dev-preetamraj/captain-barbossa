"""The captain inside the supervisor: owned and fed by it, interrupted with the crew, its native
prompts put to the user, told what the user did, and given Herdr-free instructions."""

import contextlib
import json
import os
import re
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
    do,
    eventlog,
    instructions,
    protocol,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError

# A native that holds until its input closes, then exits like a stdio server.
NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


def fake_captain(rung, state, holder):
    """A captain adapter recording every ring; `state` decides idle, working or busy."""

    def ring(link, message_id, text):
        if state["value"] == "busy":
            return {"landed": False, "gate": "agent not idle"}
        rung.append((message_id, text))
        return {"landed": True, "gate": None}

    return types.SimpleNamespace(
        open=lambda link: holder.update(link=link),
        state=lambda link: "working" if state["value"] == "busy" else state["value"],
        ring=ring,
        interrupt=lambda link, reason: "interrupted",
        set_model=lambda link, model: {"model": model, "applies": "now"},
    )


class SupervisedCaptainTests(SessionCase):
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
        self.enterContext(patch.object(supervisor, "BRIDGE_POLL", 0.05))
        self.rung, self.state, self.holder = [], {"value": "idle"}, {}
        self.enterContext(
            patch.dict(
                supervisor.ADAPTERS, {"fake": fake_captain(self.rung, self.state, self.holder)}
            )
        )
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
        self.stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, self.stopped)

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 30)
        self.addCleanup(self.stopped.set)
        with os.fdopen(read_fd) as lines:
            self.ready = json.loads(lines.readline())
        os.environ[supervision.TOKEN_ENV] = self.ready["token"]

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def until(self, check, what):
        deadline = time.monotonic() + 10
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)
        return found

    def actions(self):
        eventlog.flush_actions(self.current.directory)
        return eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)[0]

    def test_the_supervisor_owns_the_captain_and_stops_its_process_group(self):
        started = self.call("start_captain", {"provider": "fake", "model": "m"})
        self.assertEqual(started["state"], "started")
        self.assertEqual(self.call("captain_state")["state"], "idle")
        with self.assertRaisesRegex(CaptainError, r"\[captain-running\]"):
            self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.call("stop_captain"), {"outcome": "stopped"})
        self.assertEqual(self.call("captain_state"), {"state": None})
        states = [a["state"] for a in self.actions() if a["kind"] == "lifecycle"]
        self.assertEqual(states, ["started", "stopped"])
        with self.assertRaisesRegex(CaptainError, r"\[captain-not-running\]"):
            self.call("message_captain", {"text": "hi"})

    def test_operator_notes_reach_the_captain_before_its_next_turn(self):
        self.call("start_captain", {"provider": "fake"})
        self.state["value"] = "busy"
        interrupted = self.call("interrupt_all")["crews"]
        self.assertEqual(interrupted, [{"crew": "captain", "outcome": "interrupted"}])
        self.state["value"] = "idle"
        self.assertTrue(self.call("message_captain", {"text": "Carry on."})["landed"])
        _, text = self.rung[-1]
        self.assertTrue(text.startswith("Operator notes:"))
        self.assertIn("interrupted every working crew and you", text)
        self.assertTrue(text.endswith("Carry on."))
        self.call("message_captain", {"text": "Next."})
        self.assertEqual(self.rung[-1][1], "Next.")

    def test_native_prompts_of_the_captain_are_put_to_the_user(self):
        self.call("start_captain", {"provider": "fake"})
        link = self.holder["link"]
        self.assertTrue(link.captain)
        replies = {}
        approval = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": "/x"}
        threading.Thread(
            target=lambda: replies.update(approval=link.relay(approval)), daemon=True
        ).start()
        threading.Thread(
            target=lambda: replies.update(question=link.ask("question", {"questions": []})),
            daemon=True,
        ).start()
        requests = self.until(
            lambda: len(rows := self.call("captain_requests")["requests"]) == 2 and rows,
            "two open requests",
        )
        kinds = {row["kind"]: row["id"] for row in requests}
        self.assertEqual(requests[0]["state"], "open")
        with self.assertRaisesRegex(CaptainError, r"\[captain-request-answer\]"):
            self.call("answer_captain", {"request": kinds["approval"], "decision": "maybe"})
        self.call("answer_captain", {"request": kinds["approval"], "decision": "approve"})
        self.call("answer_captain", {"request": kinds["question"], "answers": {"q1": "Yes."}})
        self.until(lambda: len(replies) == 2, "both answers")
        behavior = replies["approval"][1]["hookSpecificOutput"]["decision"]["behavior"]
        self.assertEqual(behavior, "allow")
        self.assertEqual(replies["question"], {"q1": {"answers": ["Yes."]}})
        with self.assertRaisesRegex(CaptainError, r"\[captain-request-closed\]"):
            self.call("answer_captain", {"request": kinds["approval"], "decision": "deny"})
        asked = [a for a in self.actions() if a["kind"] == "question" and a["crew"] == "captain"]
        self.assertEqual(
            sorted(a["state"] for a in asked), ["answered", "answered", "asked", "asked"]
        )

    def test_the_captain_learns_when_its_authorization_is_decided(self):
        row = do.request_authorization(self.current.directory, do.parse(["do", "push"]), "captain")
        do.resolve_authorization(self.current.directory, row["id"], "approve")
        self.call("start_captain", {"provider": "fake"})
        message_id, text = self.until(lambda: self.rung and self.rung[0], "the approval message")
        self.assertEqual(message_id, f"authorization-{row['id']}-approved")
        self.assertIn("run that exact command again now", text)
        self.until(self.notified, "the request marked notified")
        time.sleep(0.2)
        self.assertEqual(len(self.rung), 1)  # told once

    def notified(self):
        rows = store.read_json(self.current.directory / "protocol.json")["authorizations"]
        return rows[0].get("notified_at")


class EscalateTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))

    def crew(self, current, execution):
        record = {"id": "will", "agent": "c-will", "incarnation_id": "b" * 32}
        record["execution"] = execution
        with sessions.crew_meta(current.directory) as meta:
            meta["crew"]["will"] = record
        crew = Crew("will", record, current._replace(meta=meta))
        assignment = protocol.begin(crew, self.project, "Map the repo.")["id"]
        asked = protocol.change(
            crew,
            types.SimpleNamespace(
                command="ask", assignment=assignment, incarnation=None, question="Which?"
            ),
            self.project,
        )
        return crew, asked["question_id"]

    def test_the_captain_routes_a_question_to_the_user_only_in_an_app_session(self):
        current = supervisor.open_session(self.project, None)
        crew, question = self.crew(current, "supervised")
        with self.assertRaisesRegex(CaptainError, r"\[stale-question-id\]"):
            protocol.escalate(crew, "other")
        routed = protocol.escalate(crew, question)
        self.assertEqual(routed["waiting_on"], "human")
        self.assertEqual(
            protocol.waiting_on(protocol.active_assignment(crew)["question"], None), "human"
        )
        herdr = sessions.session(self.project, self.pane, create=True)
        other, other_question = self.crew(herdr, None)
        with self.assertRaisesRegex(CaptainError, r"\[escalate-supervised\]"):
            protocol.escalate(other, other_question)

    def test_bridge_notes_are_marked_delivered_only_with_the_message(self):
        current = supervisor.open_session(self.project, None)
        captain_bridge.add_note(current.directory, ["tell", "will", "hi"])
        notes = captain_bridge.pending_notes(current.directory)
        self.assertEqual(notes[0]["text"], "The user ran: captain tell will hi")
        captain_bridge.notes_delivered(current.directory, notes)
        self.assertEqual(captain_bridge.pending_notes(current.directory), [])


HERDR_ONLY = re.compile(
    r"Herdr(?! Codex crew)|herdr|\bpanes?\b|\btabs?\b|\bfocus\b|background (crew|turn)"
    r"|--background|--placement|CAPTAIN wait"
)


class SupervisedInstructionTests(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_supervised_text_swaps_herdr_lines_and_never_grows(self):
        for role in ("Captain Barbossa", "crew member Jack"):
            for provider in ("codex", "claude", "pi"):
                with self.subTest(role=role, provider=provider):
                    herdr = instructions.agent_instructions(self.directory, role, provider)
                    app = instructions.agent_instructions(
                        self.directory, role, provider, supervised=True
                    )
                    self.assertLessEqual(len(app), len(herdr))
                    leftover = [line for line in app.splitlines() if HERDR_ONLY.search(line)]
                    self.assertEqual(leftover, [])

    def test_only_the_supervised_captain_learns_escalate(self):
        captain = instructions.agent_instructions(self.directory, "Captain Barbossa", "codex")
        self.assertNotIn("escalate", captain)
        app = instructions.agent_instructions(
            self.directory, "Captain Barbossa", "codex", supervised=True
        )
        self.assertIn("CAPTAIN escalate 'NAME' QUESTION_ID", app)
        self.assertIn("[--verify TARGET]", app)

    def test_every_swap_matches_the_herdr_text_it_replaces(self):
        captain = instructions.agent_instructions(self.directory, "Captain Barbossa", "codex")
        crew = instructions.agent_instructions(self.directory, "crew member Jack", "codex")
        for old, new in instructions.SUPERVISED_SWAPS:
            with self.subTest(old=old[:40]):
                self.assertTrue(old in captain or old in crew)
                self.assertLessEqual(len(new), len(old))


if __name__ == "__main__":
    unittest.main()
