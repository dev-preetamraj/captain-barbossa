"""Recovery includes context and pending coordination without consuming it."""

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import cli, instructions, memory, protocol, recovery, sessions, store
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase


class RecoveryDigestTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "original task", ["src"])
        for scope, directory in (
            ("session", self.current.directory),
            ("project", store.state_storage(self.project)),
        ):
            memory.add_memory(directory / "graph.json", scope, "decision", "full text " * 200)
        memory.add_repo_memory(self.project, "team", "convention", "test", "regression coverage")

    def ask(self):
        return protocol.change(
            self.crew,
            SimpleNamespace(
                command="ask", assignment=self.assignment["id"], question="Which file? " * 200
            ),
            self.project,
        )

    def test_all_scopes_paths_questions_delivery_ids_and_mail_without_writes(self):
        question = self.ask()
        pending = protocol.poll(self.crew, modal=False)
        protocol.enqueue(self.crew, "answer waiting for receipt", "answer", self.assignment["id"])
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(store, "write_json", side_effect=AssertionError("write")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
        digest = json.loads(output.getvalue())
        self.assertEqual(set(digest["memory"]), {"session", "project", "repo"})
        stored = memory.read_scoped_graphs(self.current.directory, self.project)["session"]
        self.assertEqual(digest["memory"]["session"][0]["target"], stored["nodes"][-1]["label"])
        self.assertEqual(digest["memory"]["repo"][0]["rationale"], "regression coverage")
        self.assertEqual(digest["assignments"][0]["task"], "original task")
        self.assertEqual(digest["owned_paths"][0]["path"], "src")
        self.assertEqual(digest["questions"][0]["id"], question["question_id"])
        self.assertEqual(digest["deliveries"], [pending])
        self.assertEqual(digest["mail"][0]["text"], "answer waiting for receipt")
        self.assertTrue(all(count == 0 for count in digest["omitted"].values()))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(recovery.digest(self.current, self.project), digest)

    def test_unsurfaced_report_remains_until_ack_even_when_assignment_is_done(self):
        report = "complete report " * 200
        protocol.change(
            self.crew,
            SimpleNamespace(command="done", assignment=self.assignment["id"], report=report),
            self.project,
        )
        digest = recovery.digest(self.current, self.project)
        self.assertEqual(digest["assignments"], [])
        self.assertEqual(digest["owned_paths"], [])
        self.assertEqual(digest["deliveries"][0]["summary"], report)
        self.assertIsNone(digest["deliveries"][0]["delivery_id"])
        pending = protocol.poll(self.crew, modal=False)
        self.assertEqual(recovery.digest(self.current, self.project)["deliveries"], [pending])
        protocol.poll(self.crew, pending["delivery_id"], modal=False)
        self.assertEqual(recovery.digest(self.current, self.project)["deliveries"], [])

    def test_each_section_reports_exact_omissions(self):
        self.ask()
        with patch.object(recovery, "LIMIT", 0):
            digest = recovery.digest(self.current, self.project)
        self.assertEqual(
            digest["omitted"],
            {
                "memory_project": 1,
                "memory_session": 1,
                "memory_repo": 1,
                "assignments": 1,
                "owned_paths": 1,
                "questions": 1,
                "deliveries": 1,
                "mail": 0,
                "lifetimes": 0,
                "results": 0,
                "worktrees": 0,
            },
        )
        self.assertEqual(digest["deliveries"], [])

    def test_empty_roster_and_crew_access(self):
        self.current.meta["crew"] = {}
        store.write_json(self.current.meta_path, self.current.meta)
        digest = recovery.digest(self.current, self.project)
        self.assertEqual(digest["assignments"], [])
        self.assertEqual(digest["deliveries"], [])
        with (
            patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 1)

    def test_dismissed_crew_contributes_no_assignment_paths_questions_deliveries_or_mail(self):
        self.ask()
        protocol.poll(self.crew, modal=False)
        protocol.enqueue(self.crew, "unread mail", "tell", self.assignment["id"])
        self.crew.record["status"] = "dismissed"
        with patch.object(
            protocol, "active_assignment", side_effect=AssertionError("dismissed read")
        ):
            digest = recovery.digest(self.current, self.project)
        for section in ("assignments", "owned_paths", "questions", "deliveries", "mail"):
            self.assertEqual(digest[section], [])
            self.assertEqual(digest["omitted"][section], 0)

    def test_repository_layout_names_recovery_module_and_command(self):
        rulebook = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text()
        layout = rulebook.split("## Layout", 1)[1].split("```text", 1)[1].split("```", 1)[0]
        self.assertIn("recovery.py", layout)
        verbs = layout.split("cli.py", 1)[1].split("agents.py", 1)[0]
        self.assertIn("recover", verbs)

    def test_generated_captain_uses_recovery_and_keeps_crew_scoped(self):
        captain = instructions.agent_instructions(self.current.directory, "Captain Barbossa")
        crew = instructions.agent_instructions(self.current.directory, "crew member Jack")
        self.assertIn("CAPTAIN recover", captain)
        self.assertIn("Read omitted memory: CAPTAIN memory show --json", captain)
        self.assertNotIn("Omission counts are explicit;", captain)
        self.assertIn("wait --all --json", captain)
        self.assertNotIn("CAPTAIN recover", crew)
        self.assertIn("memory show --scope repo", crew)
