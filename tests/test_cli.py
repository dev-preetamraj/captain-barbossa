"""Tests for the `captain` CLI surface: what cli.main does with a crew assignment.

The assignment, mail and ring state machine itself is tests/test_protocol.py; these
drive the commands a captain and crew actually type.
"""

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, dashboard, models, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class CliCommandTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.directory = self.current.directory
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "provider": "codex", "incarnation_id": "first"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "agent_status", return_value=None))
        self.enterContext(patch.object(Pane, "choice_modal", return_value=False))
        # nudge/nudge_block are pane.py's, built concurrently; stub the doorbell for delivery.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None, create=True))
        self.enterContext(patch.object(Pane, "nudge", create=True))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def saved(self):
        return store.read_json(self.directory / "protocol.json")["assignments"][
            self.assignment["id"]
        ]

    def command(self, command, **values):
        args = SimpleNamespace(
            command=command, assignment=self.assignment["id"], incarnation="first", **values
        )
        return protocol.change(self.crew, args, self.project)

    def finish(self, report="src/a.py changed; checks pass; nothing left"):
        """Done with its notification acknowledged, which is what dismissal requires."""
        self.command("done", report=report)
        protocol.poll(self.crew, protocol.poll(self.crew)["delivery_id"])

    def run_cli(self, *arguments):
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["--session", self.current.meta["id"], *arguments])
        return code, output.getvalue()

    def test_cli_inbox_prints_queued_mail_oldest_first_and_marks_it_read(self):
        with patch.dict(
            os.environ,
            {
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": "first",
                "CAPTAIN_ASSIGNMENT": self.assignment["id"],
            },
        ):
            code, output = self.run_cli("inbox", "Jack")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "No mail.")
        protocol.deliver(self.crew, "original", initial=True)
        protocol.deliver(self.crew, "followup")
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            code, output = self.run_cli("inbox", "Jack")
        self.assertEqual(code, 0)
        self.assertIn("original", output)
        self.assertIn("followup", output)
        self.assertLess(output.index("original"), output.index("followup"))
        self.assertEqual(protocol.unread(self.crew), [])
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "will"}):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.run_cli("inbox", "Jack")[0], 1)
        self.assertIn("only on its own assignment", error.getvalue())

    def test_result_mutations_and_worktree_cleanup_are_captain_only(self):
        commands = [
            ["result", "collect", "a" * 32],
            ["result", "show", "b" * 32],
            ["result", "apply", "b" * 32],
            ["result", "discard", "b" * 32, "--reason", "user discarded"],
            ["worktree", "cleanup", "a" * 32],
        ]
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}):
            for command in commands:
                with (
                    self.subTest(command=command),
                    self.assertRaisesRegex(CaptainError, "captain-only"),
                ):
                    cli.guard_crew(cli.parser().parse_args(command))

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
            message_id = protocol.deliver(
                self.crew, "src/a.py", question_id=json.loads(output)["question_id"]
            )
            protocol.receipt(self.crew.session.directory, self.crew.crew_id, [message_id])
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

    def test_handoff_inherits_owned_paths_and_actions_unless_named(self):
        self.finish()
        self.assertEqual(
            self.run_cli(
                "assign", "Jack", "--task", "replacement", "--handoff", self.assignment["id"]
            )[0],
            0,
        )
        replacement = store.read_json(self.directory / "protocol.json")["active"][self.crew.crew_id]
        crew_env = {
            "CAPTAIN_ROLE": "crew",
            "CAPTAIN_CREW": "jack",
            "CAPTAIN_INCARNATION": "first",
            "CAPTAIN_ASSIGNMENT": replacement,
        }
        with patch.dict(os.environ, crew_env):
            self.assertEqual(self.run_cli("check", "Jack", "edit", "src/a.py")[0], 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_cli("check", "Jack", "edit", "docs/a.md")[0], 1)
        self.assertEqual(self.saved()["original_task"], "original")

    def test_handoff_named_owns_and_allow_replace_inherited_ones(self):
        self.finish()
        self.assertEqual(
            self.run_cli(
                "assign",
                "Jack",
                "--task",
                "replacement",
                "--handoff",
                self.assignment["id"],
                "--owns",
                "docs",
                "--allow",
                "test",
            )[0],
            0,
        )
        replacement = store.read_json(self.directory / "protocol.json")["active"][self.crew.crew_id]
        stored = store.read_json(self.directory / "protocol.json")["assignments"][replacement]
        self.assertEqual(stored["paths"], ["docs"])
        self.assertEqual(stored["actions"], ["read", "search", "test"])

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
                    # One exemption: a merely stale assignment no longer blocks `ask`,
                    # the crew's only escalation channel (see StaleAssignmentTests). A
                    # foreign crew, wrong incarnation or missing id still reject it.
                    if (key, value, command[0]) == ("CAPTAIN_ASSIGNMENT", "old", "ask"):
                        continue
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

    def test_unacknowledged_delivery_replays_until_ack_advances_the_cursor(self):
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
        code, repeated = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(repeated), event)
        self.assertEqual(self.saved()["ack_seq"], 0)
        self.assertEqual(self.saved()["offset"], 0)
        code, _ = self.run_cli(
            "answer", "Jack", question, "src/a.py", "--assignment", self.assignment["id"]
        )
        self.assertEqual(code, 0)
        self.crew.events.parent.mkdir(exist_ok=True)
        with self.crew.events.open("a") as stream:
            stream.write(json.dumps({"hook_event_name": "Stop"}) + "\n")
        code, output = self.run_cli(
            "wait", "Jack", "--json", "--ack", event["delivery_id"], "--timeout", "0"
        )
        self.assertEqual(code, 0)
        native = json.loads(output)
        self.assertEqual(native["status"], "idle")
        self.assertNotEqual(native["delivery_id"], event["delivery_id"])
        self.assertEqual(self.saved()["ack_seq"], 1)
        self.assertEqual(self.saved()["offset"], 0)
        code, repeated = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(repeated), native)
        self.assertEqual(self.saved()["offset"], 0)
        code, output = self.run_cli(
            "wait", "Jack", "--json", "--ack", native["delivery_id"], "--timeout", "0"
        )
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["delivery_id"])
        self.assertEqual(self.saved()["offset"], self.crew.events.stat().st_size)
        with patch.dict(
            os.environ,
            {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack", "CAPTAIN_INCARNATION": "first"},
        ):
            self.run_cli("inbox", "Jack")
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

    def test_fleet_wait_returns_full_reports_questions_and_approvals_with_per_crew_acks(self):
        report = "report " * 400
        question = "question " * 300
        command = "echo " + "x" * 2000
        self.command("done", report=report)
        crews = {}
        for name in ("Will", "Gibbs"):
            crew = Crew(
                name.lower(),
                {"name": name, "agent": name.lower(), "provider": "claude", "incarnation_id": name},
                self.current,
            )
            self.current.meta["crew"][crew.crew_id] = crew.record
            assignment = protocol.begin(crew, self.project, "task")
            crews[name] = (crew, assignment)
        store.write_json(self.current.meta_path, self.current.meta)
        crew, assignment = crews["Will"]
        protocol.change(
            crew,
            SimpleNamespace(command="ask", assignment=assignment["id"], question=question),
            self.project,
        )
        crew = crews["Gibbs"][0]
        crew.events.parent.mkdir(exist_ok=True)
        crew.events.write_text(
            json.dumps({"hook_event_name": "PermissionRequest", "tool_input": {"command": command}})
            + "\n"
        )
        code, output = self.run_cli("wait", "--all", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        fleet = json.loads(output)
        deliveries = {row["crew"]: row for row in fleet["deliveries"]}
        self.assertEqual(set(deliveries), {"Jack", "Will", "Gibbs"})
        self.assertEqual(deliveries["Jack"]["summary"], report)
        self.assertTrue(deliveries["Will"]["summary"].endswith(question))
        self.assertTrue(deliveries["Gibbs"]["summary"].endswith(command))
        single = json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1])
        self.assertEqual(single, deliveries["Jack"])
        self.assertEqual(
            json.loads(self.run_cli("wait", "--all", "--json", "--timeout", "0")[1]), fleet
        )
        self.run_cli("wait", "Jack", "--ack", single["delivery_id"], "--timeout", "0")
        after = json.loads(self.run_cli("wait", "--all", "--json", "--timeout", "0")[1])
        self.assertEqual(after["deliveries"], [deliveries["Gibbs"], deliveries["Will"]])
        self.assertEqual(self.saved()["ack_seq"], 1)

    def test_fleet_wait_timeout_and_invalid_ack_do_not_acknowledge(self):
        self.assertEqual(
            json.loads(self.run_cli("wait", "--all", "--json", "--timeout", "0")[1]),
            {"status": "timeout", "deliveries": []},
        )
        self.command("done", report="finished")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("wait", "--all", "--ack", "bad")[0], 1)
        self.assertEqual(self.saved()["ack_seq"], 0)

    def test_full_done_report_restores_old_truncated_notice_and_pending_delivery(self):
        report = "old full report " * 200
        self.command("done", report=report)
        with protocol.checkpoint(self.directory) as state:
            state["assignments"][self.assignment["id"]]["notices"][0]["summary"] = report[:1600]
        first = json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1])
        self.assertEqual(first["summary"], report)
        with protocol.checkpoint(self.directory) as state:
            state["assignments"][self.assignment["id"]]["pending"]["result"]["summary"] = report[
                :1600
            ]
        self.assertEqual(
            json.loads(self.run_cli("wait", "Jack", "--json", "--timeout", "0")[1]), first
        )

    def test_fleet_poll_error_does_not_hide_another_crews_delivery(self):
        will = Crew(
            "will",
            {"name": "Will", "agent": "will", "provider": "codex", "incarnation_id": "will"},
            self.current,
        )
        self.current.meta["crew"]["will"] = will.record
        store.write_json(self.current.meta_path, self.current.meta)
        assignment = protocol.begin(will, self.project, "task")
        real_poll = protocol.poll
        error = "poll failed " * 200

        def poll(crew, **kwargs):
            if crew.crew_id == "jack":
                raise CaptainError(error)
            return real_poll(crew, **kwargs)

        protocol.change(
            will,
            SimpleNamespace(command="done", assignment=assignment["id"], report="Will report"),
            self.project,
        )
        with patch.object(protocol, "poll", side_effect=poll):
            code, output = self.run_cli("wait", "--all", "--json", "--timeout", "0")
        self.assertEqual(code, 0)
        fleet = json.loads(output)
        self.assertEqual(fleet["status"], "ready")
        failed, delivered = fleet["deliveries"]
        self.assertEqual(failed, protocol.result(self.crew, self.assignment, "error", error))
        self.assertEqual(delivered["crew"], "Will")
        self.assertEqual(delivered["summary"], "Will report")
        self.assertIsNotNone(delivered["delivery_id"])
        self.assertEqual(self.saved()["ack_seq"], 0)

    def test_fleet_json_errors_retain_fleet_shape(self):
        code, output = self.run_cli("wait", "--all", "--json", "--ack", "invalid")
        self.assertEqual(code, 1)
        failure = json.loads(output)
        self.assertEqual(failure["status"], "error")
        self.assertEqual(failure["deliveries"], [])
        self.assertNotIn("crew", failure)
        self.assertIn("takes no name or ack", failure["summary"])
        with (
            patch.object(cli, "current_pane", side_effect=CaptainError("no workspace")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["--session", self.current.meta["id"], "wait", "--all", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["deliveries"], [])

    def test_fleet_wait_skips_legacy_dismissed_and_stale_incarnations(self):
        self.command("done", report="finished")
        for name, extras in (
            ("Will", {}),
            ("Gibbs", {"incarnation_id": "gibbs", "status": "dismissed"}),
        ):
            self.current.meta["crew"][name.lower()] = {
                "name": name,
                "agent": name.lower(),
                "provider": "codex",
                **extras,
            }
        store.write_json(self.current.meta_path, self.current.meta)
        deliveries = json.loads(self.run_cli("wait", "--all", "--json", "--timeout", "0")[1])[
            "deliveries"
        ]
        self.assertEqual([row["crew"] for row in deliveries], ["Jack"])
        self.current.meta["crew"]["jack"]["incarnation_id"] = "replacement"
        store.write_json(self.current.meta_path, self.current.meta)
        self.assertEqual(
            json.loads(self.run_cli("wait", "--all", "--json", "--timeout", "0")[1])["deliveries"],
            [],
        )

    def test_cli_json_error_is_one_object_and_does_not_adopt_legacy(self):
        del self.current.meta["crew"]["jack"]["incarnation_id"]
        store.write_json(self.current.meta_path, self.current.meta)
        before = (self.directory / "protocol.json").read_bytes()
        code, output = self.run_cli("wait", "Jack", "--json", "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["status"], "error")
        self.assertEqual((self.directory / "protocol.json").read_bytes(), before)

    def test_cli_scoped_inspection_and_session_need_no_herdr_or_state_writes(self):
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(store, "write_text", side_effect=AssertionError("state write")),
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

    def test_models_command_lists_one_providers_ids_aliases_and_tiers_with_no_pane(self):
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["models", "--agent", "claude"]), 0)
        text = output.getvalue()
        self.assertIn("claude-sonnet-5-5 (sonnet)", text)
        self.assertIn(
            "tiers: cheap=claude-haiku-5-5 mid=claude-sonnet-5-5 strong=claude-opus-5-5", text
        )

    def test_models_command_with_no_agent_lists_every_provider_and_tolerates_pi_failure(self):
        with (
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(
                models, "pi_models", side_effect=CaptainError("pi --list-models failed: boom")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["models"]), 0)
        text = output.getvalue()
        for header in ("claude:", "codex:", "pi:", "grok:"):
            self.assertIn(header, text)
        self.assertIn("pi --list-models failed", text)
        self.assertIn("gpt-6-astra (astra)", text)

    def test_status_read_does_not_create_state_or_call_native_done_completion(self):
        before = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        args = SimpleNamespace(session=self.current.meta["id"], all=False)
        with (
            patch.object(agents.runtime, "herdr", return_value={"agent": {"agent_status": "done"}}),
            patch("captain_barbossa.pane.Pane.lines", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.status_crew(args, self.pane, self.project)
        self.assertIn("state:unknown source:none", output.getvalue())
        self.assertNotIn("state:done", output.getvalue())
        self.assertEqual(
            {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}, before
        )

    def test_dashboard_price_refresh_requires_an_explicit_flag(self):
        for refresh in (False, True):
            args = SimpleNamespace(
                session=self.current.meta["id"], interval=None, refresh_prices=refresh
            )
            with (
                patch.object(dashboard.usage, "_prices") as prices,
                patch.object(dashboard, "run"),
            ):
                dashboard.run_dashboard(args, self.pane, self.project)
            self.assertEqual(prices.call_count, int(refresh))
            if refresh:
                prices.assert_called_once_with(cached_only=False)

    def test_model_switch_checks_composer_before_terminal_input(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack", model="mid")
        with patch.object(agents.runtime, "herdr") as send:
            with self.assertRaisesRegex(CaptainError, "empty composer"):
                agents.switch_model(args, self.pane, self.project)
        send.assert_not_called()

    def test_dismiss_missing_pane_retires_record_and_releases_paths(self):
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        self.finish()
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(
            agents.runtime, "herdr", side_effect=CaptainError('Herdr: {"code": "pane_not_found"}')
        ) as close:
            agents.dismiss_crew(args, self.pane, self.project)
        close.assert_called_once_with("pane", "close", "w1:p2")
        current = sessions.read_session(self.project, self.current.meta["id"], self.pane)
        self.assertEqual(current.meta["crew"]["jack"]["status"], "dismissed")
        self.assertEqual(self.saved()["state"], "done")
        self.assertFalse(Crew.name_reserved(current, "jack"))
        replacement = Crew(
            "will", {"name": "Will", "agent": "will", "incarnation_id": "second"}, current
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_dismiss_other_close_failure_preserves_record_and_assignment(self):
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        self.finish()
        before = self.saved()
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", side_effect=CaptainError("permission_denied")),
            self.assertRaisesRegex(CaptainError, "Could not dismiss Jack: permission_denied"),
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        current = sessions.read_session(self.project, self.current.meta["id"], self.pane)
        self.assertEqual(current.meta["crew"]["jack"], self.crew.record)
        self.assertEqual(self.saved(), before)

    def test_dismiss_releases_an_assignment_whose_mail_was_never_read(self):
        """Mail the crew never read cannot demand a done report; an enqueue is not a read."""
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        # A held ring: a landed body would count as delivered.
        with patch.object(Pane, "nudge_block", return_value="agent not idle"):
            protocol.deliver(self.crew, "original", initial=True)
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(agents.runtime, "herdr", return_value={}):
            agents.dismiss_crew(args, self.pane, self.project)
        saved = self.saved()
        self.assertEqual(saved["state"], "done")
        self.assertEqual(saved["report"], "Dismissed before any message was read.")
        replacement = Crew(
            "will",
            {"name": "Will", "agent": "will", "incarnation_id": "second"},
            sessions.read_session(self.project, self.current.meta["id"], self.pane),
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_dismiss_releases_an_assignment_that_never_got_a_message(self):
        """A launch that died before its first delivery: no crew is alive to run done."""
        self.crew.record.update(pane="w1:p2", assignment_id=self.assignment["id"])
        store.write_json(self.current.meta_path, self.current.meta)
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with patch.object(agents.runtime, "herdr", return_value={}):
            agents.dismiss_crew(args, self.pane, self.project)
        saved = self.saved()
        self.assertEqual(saved["state"], "done")
        self.assertEqual(saved["report"], "Dismissed before any message was read.")
        replacement = Crew(
            "will",
            {"name": "Will", "agent": "will", "incarnation_id": "second"},
            sessions.read_session(self.project, self.current.meta["id"], self.pane),
        )
        protocol.begin(replacement, self.project, "replacement", ["src"], ["edit"])

    def test_failed_launch_preserves_a_retryable_launcher(self):
        args = cli.parser().parse_args(
            [
                "--session",
                self.current.meta["id"],
                "crew",
                "will",
                "--agent",
                "codex",
                "--task",
                "build",
                "--placement",
                "pane",
                "--direction",
                "vertical",
                "--split-pane",
                "w1:p1",
            ]
        )

        def herdr(*call, **kwargs):
            if call[:2] == ("pane", "split"):
                return {
                    "pane": {"pane_id": "w1:p2", "terminal_id": "crew-terminal", "tab_id": "w1:t1"}
                }
            if call[:2] == ("pane", "run"):
                self.fail("launcher was typed into an unsettled shell")
            return {}

        with (
            patch.object(agents.runtime, "herdr", side_effect=herdr),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(agents, "shell_ready_for_input", return_value=False) as ready,
            self.assertRaisesRegex(CaptainError, "launcher .* preserved.*retried") as error,
        ):
            agents.create_crew(args, self.pane, self.project)
        ready.assert_called_once_with("w1:p2")
        launcher = self.directory / "crew-will.sh"
        self.assertTrue(launcher.exists())
        self.assertNotIn("rm -f", launcher.read_text(encoding="utf-8"))
        crew_agent = sessions.agent_name(self.current.meta["id"], "will")
        self.assertIn(f"/bin/sh {launcher}", str(error.exception))
        self.assertIn(f"herdr agent rename w1:p2 {crew_agent}", str(error.exception))
        self.assertIn(f"herdr agent get {crew_agent}", str(error.exception))


if __name__ == "__main__":
    unittest.main()
