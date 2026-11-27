from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import eventlog, protocol, results, sessions, store, supervision, worktrees
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase


class ActionLogTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.current.meta["transport"] = "supervisor"
        self.crew = Crew(
            "jack",
            {
                "id": "jack",
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "model": "test-model",
                "checkout": "shared",
                "sources": {"agent": "--agent", "model": "--model"},
                "execution": "supervised",
                "incarnation_id": "first",
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "original")

    def actions(self, kind: str) -> list[dict]:
        eventlog.flush_actions(self.current.directory)
        rows, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        return [row for row in rows if row["kind"] == kind]

    def test_recruit_records_effective_choices_and_sources(self) -> None:
        row = self.actions("recruit")[0]
        for key in ("provider", "model", "checkout", "sources"):
            self.assertEqual(row[key], self.crew.record[key])

    def test_tell_and_answer_record_the_original_actor(self) -> None:
        for role, actor in (("captain", "captain"), ("", "human")):
            with self.subTest(role=role), patch.dict(os.environ, {"CAPTAIN_ROLE": role}):
                protocol.deliver(self.crew, f"followup {role}", queue_only=True)
                with protocol.checkpoint(self.current.directory) as state:
                    assignment = protocol.active(state, self.crew)
                    assignment["question"] = {"id": f"question-{actor}"}
                protocol.deliver(
                    self.crew,
                    f"answer {role}",
                    question_id=f"question-{actor}",
                    queue_only=True,
                )
                rows = self.actions("mail")[-2:]
                self.assertEqual([row["actor"] for row in rows], [actor, actor])
                self.assertEqual([row["mail_kind"] for row in rows], ["followup", "answer"])

    def test_busy_mail_logs_one_hold_per_gate_transition(self) -> None:
        message = protocol.deliver(self.crew, "wait for this", queue_only=True)
        with patch.object(supervision, "ring", return_value={"landed": False, "gate": "busy"}):
            protocol.ring(self.crew, message)
            protocol.ring(self.crew, message)
        rows = self.actions("mail")
        self.assertEqual([row["state"] for row in rows], ["sent", "held"])
        self.assertEqual(rows[-1]["gate"], "busy")
        self.assertEqual(rows[-1]["message"], message)
        with patch.object(supervision, "ring", return_value={"landed": True, "gate": None}):
            protocol.ring(self.crew, message)
        protocol.receipt(self.current.directory, self.crew.crew_id, [message])
        self.assertEqual(self.actions("mail")[-1]["state"], "read")

    def test_grants_keep_gate_reason_and_crew_words_at_resolution(self) -> None:
        for decision in ("grant", "deny"):
            with self.subTest(decision=decision):
                protocol.note_denial(
                    self.crew,
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": f"git push origin {decision}"},
                        "approval": {"code": "flagged-outward"},
                    },
                )
                with protocol.checkpoint(self.current.directory) as state:
                    assignment = protocol.active(state, self.crew)
                    question = {"id": decision, "text": f"May I {decision}?"}
                    assignment["question"] = question
                    request = protocol.grant_request(self.crew, assignment, question)
                with patch.object(protocol, "ring"):
                    protocol.resolve_request(self.crew, request["id"], decision, "human decision")
                rows = self.actions("grant")[-2:]
                self.assertEqual([row["words"] for row in rows], [question["text"]] * 2)
                self.assertEqual([row["gate_reason"] for row in rows], [request["reason"]] * 2)

    def test_herdr_mail_and_grants_write_no_action_files(self) -> None:
        self.current.meta.pop("transport")
        other = self.root / "herdr"
        other.mkdir()
        current = self.current._replace(directory=other)
        store.write_json(current.meta_path, current.meta)
        crew = Crew("jack", self.crew.record, current)
        protocol.begin(crew, self.project, "ordinary")
        protocol.deliver(crew, "ordinary mail", queue_only=True)
        protocol.grant(crew, protocol.active_assignment(crew)["id"], "git push")
        self.assertFalse((other / eventlog.ACTIONS).exists())
        self.assertFalse((other / eventlog.ACTION_SPOOL).exists())

    def test_result_collection_apply_and_discard_log_durable_transitions(self) -> None:
        for argv in (
            ("init",),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.invalid"),
            ("commit", "--allow-empty", "-m", "base"),
        ):
            subprocess.run(["git", *argv], cwd=self.project, check=True, capture_output=True)
        record = worktrees.reserve(self.current, "jack", "a" * 32, self.assignment["id"])
        record = worktrees.create(self.current, record["id"])
        (Path(record["execution_root"]) / "file").write_text("crew output")
        with patch("captain_barbossa.lifetime.check", return_value=SimpleNamespace(ok=True)):
            manifest = results.collect(self.current, record["id"], ["file"])
        results.apply(self.current, manifest["id"])
        results.apply(self.current, manifest["id"])
        results.discard(self.current, manifest["id"], "keep applied files")
        results.discard(self.current, manifest["id"], "keep applied files")
        rows = self.actions("result")
        self.assertEqual(
            [row["state"] for row in rows], ["collected", "applying", "applied", "discarded"]
        )
        self.assertTrue(all(row["result"] == manifest["id"] for row in rows))
        self.assertTrue(all(row["assignment"] == self.assignment["id"] for row in rows))
        self.assertEqual((self.project / "file").read_text(), "crew output")
