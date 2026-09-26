import contextlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, protocol
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError
from tests import home_isolation  # noqa: F401


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                },
            )
        )
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.current = memory.session(self.project, self.pane, create=True)
        self.directory = self.current.directory
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        memory.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "agent_status", return_value=None))
        self.enterContext(patch.object(Pane, "choice_modal", return_value=False))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def saved(self):
        return memory.read_json(self.directory / "protocol.json")["assignments"][
            self.assignment["id"]
        ]

    def command(self, command, **values):
        args = SimpleNamespace(
            command=command, assignment=self.assignment["id"], incarnation="first", **values
        )
        return protocol.change(self.crew, args, self.project)

    def event(self, event):
        self.crew.events.parent.mkdir(exist_ok=True)
        with self.crew.events.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def test_persist_before_single_send_and_preserve_original_task(self):
        def submitted(text, provider, attempts):
            saved = self.saved()
            self.assertEqual(saved["messages"][-1]["delivery"], "pending")
            self.assertTrue(
                text.startswith(f"Crew name: Jack.\nAssignment {saved['id']}; incarnation first; ")
            )
            self.assertIn(saved["messages"][-1]["id"], text)
            self.assertEqual(attempts, 1)

        with patch.object(Pane, "submit_task", side_effect=submitted):
            protocol.deliver(self.crew, "original", initial=True)
            protocol.deliver(self.crew, "followup")
        saved = self.saved()
        self.assertEqual(saved["original_task"], "original")
        self.assertEqual([m["text"] for m in saved["messages"]], ["original", "followup"])
        self.assertEqual(len({m["id"] for m in saved["messages"]}), 2)

    def test_unknown_delivery_is_durable_and_never_retried(self):
        with patch.object(Pane, "submit_task", side_effect=CaptainError("unconfirmed")) as send:
            with self.assertRaises(CaptainError):
                protocol.deliver(self.crew, "original")
            with self.assertRaisesRegex(CaptainError, "never auto-resend"):
                protocol.deliver(self.crew, "retry")
            send.assert_called_once()
        message = self.saved()["messages"][0]
        self.assertEqual(message["delivery"], "unknown")
        self.assertEqual(protocol.poll(self.crew)["status"], "delivery_unknown")
        protocol.resolve_delivery(self.crew, self.assignment["id"], message["id"], "sent")
        self.assertEqual(self.saved()["messages"][0]["delivery"], "sent")

    def test_process_exit_during_send_leaves_unknown_delivery(self):
        with patch.object(Pane, "submit_task", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                protocol.deliver(self.crew, "original")
        self.assertEqual(self.saved()["messages"][0]["delivery"], "pending")
        response = protocol.poll(self.crew)
        self.assertEqual(response["status"], "delivery_unknown")
        self.assertEqual(protocol.poll(self.crew), response)

    def test_one_question_correlated_answer_and_explicit_done(self):
        asked = self.command("ask", question="Which path?")
        with self.assertRaisesRegex(CaptainError, "one question"):
            self.command("ask", question="Another?")
        with self.assertRaisesRegex(CaptainError, "pending question"):
            self.command("done", report="finished")
        with patch.object(Pane, "submit_task") as send:
            with self.assertRaises(CaptainError):
                protocol.deliver(self.crew, "wrong", question_id="old")
            send.assert_not_called()
            protocol.deliver(self.crew, "src", question_id=asked["question_id"])
        self.command("done", report="src/a.py changed; test passed; nothing left")
        self.assertEqual(self.saved()["state"], "done")
        first = protocol.poll(self.crew)
        self.assertEqual(first["status"], "asked")
        second = protocol.poll(self.crew, first["delivery_id"])
        self.assertEqual(second["status"], "done")
        self.assertEqual(protocol.poll(self.crew), second)
        self.assertIsNone(protocol.poll(self.crew, second["delivery_id"])["delivery_id"])

    def test_native_finish_is_idle_and_offsets_wait_for_ack(self):
        self.event({"hook_event_name": "Stop"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = list(pool.map(lambda _: protocol.poll(self.crew), range(2)))
        self.assertEqual(left, right)
        self.assertEqual(left["status"], "idle")
        self.assertEqual(self.saved()["state"], "working")
        self.assertEqual(self.saved()["offset"], 0)
        with self.assertRaises(CaptainError):
            protocol.poll(self.crew, "wrong")
        self.assertEqual(protocol.poll(self.crew), left)
        protocol.poll(self.crew, left["delivery_id"])
        self.assertEqual(self.saved()["offset"], self.crew.events.stat().st_size)
        self.assertIsNone(protocol.poll(self.crew, left["delivery_id"])["delivery_id"])

    def test_stale_assignment_incarnation_and_foreign_actor_are_refused(self):
        for assignment, incarnation in (("old", "first"), (self.assignment["id"], "old")):
            with self.subTest(assignment=assignment, incarnation=incarnation):
                with protocol.checkpoint(self.directory) as state:
                    with self.assertRaises(CaptainError):
                        protocol.active(state, self.crew, assignment, incarnation)
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "will", "CAPTAIN_INCARNATION": "first"},
        ):
            with self.assertRaises(CaptainError):
                self.command("done", report="forged")

    def test_paths_actions_overlap_and_reported_handoff(self):
        self.command("check", action="edit", path=["src/file.py"])
        for action, path in (
            ("edit", ["docs/a"]),
            ("commit", ["src/a"]),
            ("edit", ["../escape"]),
            ("edit", []),
        ):
            with self.subTest(action=action, path=path), self.assertRaises(CaptainError):
                self.command("check", action=action, path=path)
        other = Crew(
            "will", {"name": "Will", "agent": "will", "incarnation_id": "second"}, self.crew.session
        )
        with self.assertRaisesRegex(CaptainError, "overlap"):
            protocol.begin(other, self.project, "task", ["src/nested"])
        with self.assertRaisesRegex(CaptainError, "handoff"):
            protocol.begin(self.crew, self.project, "new task")
        self.command("done", report="handoff: no files changed; checks passed")
        with self.assertRaisesRegex(CaptainError, "Acknowledge"):
            protocol.begin(self.crew, self.project, "new task", handoff=self.assignment["id"])
        response = protocol.poll(self.crew)
        protocol.poll(self.crew, response["delivery_id"])
        new = protocol.begin(self.crew, self.project, "new task", handoff=self.assignment["id"])
        self.assertNotEqual(new["id"], self.assignment["id"])
        self.assertEqual(self.saved()["original_task"], "original")

    def test_symlink_escape_legacy_and_event_bounds(self):
        (self.project / "escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CaptainError):
            protocol.owned_paths(self.project, ["escape/x"])
        legacy = Crew("old", {"name": "Old", "agent": "old"}, self.crew.session)
        with self.assertRaisesRegex(CaptainError, "Legacy"):
            protocol.poll(legacy)
        self.crew.events.parent.mkdir()
        self.crew.events.write_bytes(b"x" * 65536)
        with self.assertRaisesRegex(CaptainError, "64 KiB"):
            protocol.poll(self.crew)

    def test_timeout_has_fixed_json_shape_and_no_delivery(self):
        response = protocol.wait(self.crew, 0)
        self.assertEqual(
            set(response), {"status", "delivery_id", "crew", "assignment_id", "summary"}
        )
        self.assertEqual(response["status"], "timeout")
        self.assertIsNone(response["delivery_id"])

    def run_cli(self, *arguments):
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["--session", self.current.meta["id"], *arguments])
        return code, output.getvalue()

    def test_launch_assignment_defaults_and_explicit_replacement(self):
        with patch.dict(
            os.environ,
            {
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": "first",
                "CAPTAIN_ASSIGNMENT": self.assignment["id"],
            },
        ):
            self.assertEqual(self.run_cli("check", "Jack", "read")[0], 0)
            code, output = self.run_cli("ask", "Jack", "Which file?")
            self.assertEqual(code, 0)
            with patch.object(Pane, "submit_task"):
                protocol.deliver(
                    self.crew, "src/a.py", question_id=json.loads(output)["question_id"]
                )
            self.assertEqual(self.run_cli("done", "Jack", "--report", "Finished")[0], 0)
            event = protocol.poll(self.crew)
            while event["delivery_id"]:
                event = protocol.poll(self.crew, event["delivery_id"])
            replacement = protocol.begin(
                self.crew, self.project, "replacement", handoff=self.assignment["id"]
            )
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.run_cli("check", "Jack", "read")[0], 1)
            self.assertIn("Stale assignment ID", error.getvalue())
            self.assertEqual(
                self.run_cli("check", "Jack", "read", "--assignment", replacement["id"])[0], 0
            )
            self.assertEqual(os.environ["CAPTAIN_ASSIGNMENT"], self.assignment["id"])

    def test_launch_assignment_rejects_missing_stale_or_foreign_context(self):
        context = {
            "CAPTAIN_ROLE": "crew",
            "CAPTAIN_CREW": "jack",
            "CAPTAIN_INCARNATION": "first",
            "CAPTAIN_ASSIGNMENT": self.assignment["id"],
        }
        for key, value in (
            ("CAPTAIN_ASSIGNMENT", ""),
            ("CAPTAIN_ASSIGNMENT", "old"),
            ("CAPTAIN_INCARNATION", ""),
            ("CAPTAIN_INCARNATION", "old"),
            ("CAPTAIN_CREW", "will"),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {**context, key: value}),
            ):
                for command in (
                    ("check", "Jack", "read"),
                    ("ask", "Jack", "Question?"),
                    ("done", "Jack", "--report", "Finished"),
                ):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(self.run_cli(*command)[0], 1)
        self.assertEqual(self.saved()["state"], "working")

    def test_captain_cannot_default_to_launch_assignment(self):
        with patch.dict(os.environ, {"CAPTAIN_ASSIGNMENT": self.assignment["id"]}):
            for command in (
                ("check", "Jack", "read"),
                ("ask", "Jack", "Question?"),
                ("done", "Jack", "--report", "Finished"),
            ):
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(self.run_cli(*command)[0], 1)
                self.assertIn("explicit --assignment ID", error.getvalue())

    def test_cli_question_answer_done_and_ack_round_trip(self):
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            code, output = self.run_cli(
                "ask", "Jack", "Which file?", "--assignment", self.assignment["id"]
            )
        self.assertEqual(code, 0)
        question = json.loads(output)["question_id"]
        code, output = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        event = json.loads(output)
        self.assertEqual(event["status"], "asked")
        self.assertEqual(
            json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1]), event
        )
        with patch.object(Pane, "submit_task") as send:
            code, _ = self.run_cli(
                "answer", "Jack", question, "src/a.py", "--assignment", self.assignment["id"]
            )
        self.assertEqual(code, 0)
        send.assert_called_once()
        self.run_cli("wait", "Jack", "--json", "--ack", event["delivery_id"], "--timeout", "0")
        code, _ = self.run_cli(
            "done",
            "Jack",
            "--assignment",
            self.assignment["id"],
            "--report",
            "src/a.py; tests pass; nothing left",
        )
        self.assertEqual(code, 0)
        event = json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1])
        self.assertEqual(event["status"], "done")
        self.assertIn("tests pass", event["summary"])
        code, output = self.run_cli(
            "wait", "Jack", "--json", "--ack", event["delivery_id"], "--timeout", "0"
        )
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["delivery_id"])

    def test_cli_json_error_is_one_object_and_does_not_adopt_legacy(self):
        del self.current.meta["crew"]["jack"]["incarnation_id"]
        memory.write_json(self.current.meta_path, self.current.meta)
        before = (self.directory / "protocol.json").read_bytes()
        code, output = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["status"], "error")
        self.assertEqual((self.directory / "protocol.json").read_bytes(), before)

    def test_cli_scoped_inspection_and_session_need_no_herdr_or_state_writes(self):
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(memory, "write_json", side_effect=AssertionError("state write")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "session"]), 0)
            self.assertEqual(cli.main(["memory", "path", "--scope", "repo"]), 0)
            self.assertEqual(cli.main(["memory", "show", "--scope", "repo"]), 0)
            with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}):
                self.assertEqual(cli.main(["inspect", "files"]), 0)
        self.assertIn(self.current.meta["id"], output.getvalue())
        for scope in ("session", "project", "repo"):
            self.assertEqual(
                cli.parser().parse_args(["memory", "path", "--scope", scope]).scope, scope
            )

    def test_status_read_does_not_create_state_or_call_native_done_completion(self):
        before = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        args = SimpleNamespace(session=self.current.meta["id"], all=False)
        with (
            patch.object(agents.runtime, "herdr", return_value={"agent": {"agent_status": "done"}}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.status_crew(args, self.pane, self.project)
        self.assertIn("idle", output.getvalue())
        self.assertNotIn("done", output.getvalue())
        self.assertEqual(
            {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}, before
        )

    def test_dashboard_price_refresh_requires_an_explicit_flag(self):
        for refresh in (False, True):
            args = SimpleNamespace(
                session=self.current.meta["id"], interval=None, refresh_prices=refresh
            )
            with (
                patch.object(agents.usage, "_prices") as prices,
                patch.object(agents.dashboard, "run"),
            ):
                agents.run_dashboard(args, self.pane, self.project)
            self.assertEqual(prices.call_count, int(refresh))
            if refresh:
                prices.assert_called_once_with(cached_only=False)

    def test_model_switch_checks_composer_before_terminal_input(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack", model="mid")
        with patch.object(agents.runtime, "herdr") as send:
            with self.assertRaisesRegex(CaptainError, "empty composer"):
                agents.switch_model(args, self.pane, self.project)
        send.assert_not_called()

    def test_late_send_result_cannot_complete_a_newer_unknown_answer(self):
        first_question = self.command("ask", question="First?")["question_id"]

        def delayed_send(*args, **kwargs):
            first = self.saved()["messages"][-1]["id"]
            protocol.resolve_delivery(self.crew, self.assignment["id"], first, "sent")
            second_question = self.command("ask", question="Second?")["question_id"]
            with patch.object(Pane, "submit_task", side_effect=CaptainError("unknown second send")):
                with self.assertRaises(CaptainError):
                    protocol.deliver(self.crew, "second answer", question_id=second_question)

        with patch.object(Pane, "submit_task", side_effect=delayed_send):
            first = protocol.deliver(self.crew, "first answer", question_id=first_question)
        protocol.record_delivery(self.crew, self.assignment["id"], first, "unknown")
        assignment = self.saved()
        self.assertEqual([m["delivery"] for m in assignment["messages"]], ["sent", "unknown"])
        self.assertEqual(assignment["question"]["text"], "Second?")
        self.assertEqual(assignment["state"], "asked")
