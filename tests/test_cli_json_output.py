from __future__ import annotations

import contextlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, dashboard, protocol, runtime, sessions, store, supervision
from captain_barbossa.crew import Crew, CrewState
from tests.home_isolation import SessionCase


class CliJsonOutputTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_SUPERVISOR_TOKEN": "test-token"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.current.meta["transport"] = "supervisor"
        self.record = {
            "id": "jack",
            "name": "Jack",
            "agent": "jack",
            "provider": "codex",
            "model": None,
            "checkout": "shared",
            "execution": "supervised",
            "incarnation_id": "first",
            "status": "started",
            "sources": {"agent": "--agent"},
        }
        self.current.meta["crew"]["jack"] = self.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.crew = Crew("jack", self.record, self.current)
        self.assignment = protocol.begin(self.crew, self.project, "full task\nsecond line")
        self.record["assignment_id"] = self.assignment["id"]
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.object(runtime, "herdr", side_effect=AssertionError("no Herdr")))
        self.enterContext(
            patch.object(Crew, "current_state", return_value=CrewState("idle", "process", "ready"))
        )

    def invoke(self, *argv: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--session", self.current.meta["id"], *argv])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_status_json_preserves_nulls_and_full_task(self) -> None:
        response = json.loads(self.invoke("status", "--json"))
        self.assertEqual(response["session_id"], self.current.meta["id"])
        row = response["crew"][0]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["source"], "process")
        self.assertIsNone(row["model"])
        self.assertIsNone(row["pane"])
        self.assertEqual(row["task"], "full task\nsecond line")
        self.assertTrue(self.invoke("status").startswith("NAME  PROVIDER"))

    def test_empty_status_json_has_an_empty_roster(self) -> None:
        self.current.meta["crew"] = {}
        store.write_json(self.current.meta_path, self.current.meta)
        self.assertEqual(json.loads(self.invoke("status", "--json"))["crew"], [])
        self.assertEqual(self.invoke("status"), "No crew.\n")

    def test_tell_and_answer_json_keep_existing_message_identifiers(self) -> None:
        with patch.object(protocol, "ring"):
            response = json.loads(
                self.invoke(
                    "tell", "Jack", "followup", "--assignment", self.assignment["id"], "--json"
                )
            )
            self.assertEqual(response["crew"], "Jack")
            self.assertIsInstance(response["message_id"], str)
            with protocol.checkpoint(self.current.directory) as state:
                protocol.active(state, self.crew)["question"] = {"id": "question"}
            response = json.loads(
                self.invoke(
                    "answer",
                    "Jack",
                    "question",
                    "answer",
                    "--assignment",
                    self.assignment["id"],
                    "--json",
                )
            )
            self.assertEqual(response["assignment_id"], self.assignment["id"])
            self.assertIsInstance(response["message_id"], str)

    def test_interrupt_json_distinguishes_idle_from_interrupted(self) -> None:
        for state in ("idle", "interrupted"):
            with (
                self.subTest(state=state),
                patch.object(supervision, "interrupt", return_value=state),
            ):
                self.assertEqual(
                    json.loads(self.invoke("interrupt", "Jack", "--json")),
                    {"crew": "Jack", "state": state},
                )
        with patch.object(supervision, "interrupt", return_value="idle"):
            self.assertEqual(
                self.invoke("interrupt", "Jack"), "Jack was idle; nothing was interrupted.\n"
            )

    def test_model_json_reports_when_the_model_applies(self) -> None:
        with (
            patch.object(agents, "resolve_model", return_value="chosen"),
            patch.object(supervision, "set_model", return_value={"applies": "next-turn"}),
        ):
            response = json.loads(self.invoke("model", "Jack", "chosen", "--json"))
            self.assertEqual(response, {"crew": "Jack", "model": "chosen", "applies": "next-turn"})
            self.assertEqual(
                self.invoke("model", "Jack", "chosen"),
                "Jack switched to chosen; it applies from its next turn.\n",
            )

    def test_recruitment_json_has_stable_fields_without_changing_legacy_output(self) -> None:
        result = agents.recruitment_result(SimpleNamespace(json=True), self.record)
        self.assertEqual(result["crew"], "Jack")
        self.assertEqual(result["assignment_id"], self.assignment["id"])
        self.assertEqual(result["sources"], {"agent": "--agent"})
        self.assertIsNone(result["pane"])
        legacy = agents.recruitment_result(SimpleNamespace(json=False), self.record)
        self.assertEqual(legacy, {k: v for k, v in self.record.items() if k != "sources"})

    def test_dashboard_json_is_a_single_snapshot_with_numeric_usage(self) -> None:
        with (
            patch.object(supervision, "state", return_value="idle"),
            patch.object(
                dashboard,
                "with_live",
                return_value={
                    "tokens": 1234,
                    "cost": 0.5,
                    "rate": 12.3,
                    "context": 20,
                    "limit": 100,
                },
            ),
        ):
            response = json.loads(self.invoke("dashboard", "--json"))
        row = response["agents"][0]
        self.assertEqual(row["tokens"], 1234)
        self.assertEqual(row["cost"], 0.5)
        self.assertEqual(row["context_percent"], 20)
        self.assertEqual(response["total"]["tokens"], 1234)
        self.assertFalse(response["total"]["incomplete"])

    def test_dismiss_json_is_one_document_including_bounced_mail(self) -> None:
        message = protocol.deliver(self.crew, "unread", queue_only=True)
        response = json.loads(self.invoke("dismiss", "Jack", "--json"))
        self.assertEqual(response["state"], "dismissed")
        self.assertEqual(response["bounced"], [message])
        self.assertIn("before any message", response["unreported"])
        self.assertEqual(response["untracked_paths"], [])
