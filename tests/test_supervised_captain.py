"""The captain inside the supervisor: owned and fed by it, interrupted with the crew, its native
prompts put to the user, told what the user did, and given Herdr-free instructions."""

import contextlib
import io
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
    cli,
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
        if state["value"] == "broken":
            raise CaptainError("[jsonrpc-error] turn/start: the native refused.")
        rung.append((message_id, text))
        if state["value"] == "unconfirmed":
            return {"landed": False, "gate": "delivery unconfirmed"}
        return {"landed": True, "gate": None}

    def open_link(link):
        holder.update(link=link, seed=link.thread)
        link.thread = link.thread or f"thread-{len(holder.setdefault('opened', []))}"
        holder["opened"].append(link.thread)

    return types.SimpleNamespace(
        open=open_link,
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
        self.assertEqual(
            self.call("captain_state"),
            {
                "state": None,
                "provider": "fake",
                "model": "m",
                "effort": None,
                "permission_mode": None,
            },
        )
        states = [a["state"] for a in self.actions() if a["kind"] == "lifecycle"]
        self.assertEqual(states, ["started", "stopped"])
        with self.assertRaisesRegex(CaptainError, r"\[captain-not-running\]"):
            self.call("message_captain", {"text": "hi"})

    def test_a_restarted_captain_resumes_its_native_session_only_for_the_same_provider(self):
        self.enterContext(
            patch.dict(
                supervisor.ADAPTERS, {"other": fake_captain([], {"value": "idle"}, self.holder)}
            )
        )
        record = self.current.directory / "captain.json"
        self.call("start_captain", {"provider": "fake"})
        self.assertIsNone(self.holder["seed"])
        self.assertEqual(store.read_json(record)["native_session_id"], "thread-0")
        self.call("stop_captain")
        self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.holder["seed"], "thread-0")
        self.call("stop_captain")
        self.call("start_captain", {"provider": "other"})
        self.assertIsNone(self.holder["seed"])
        self.assertEqual(store.read_json(record)["provider"], "other")

    def test_the_user_acknowledges_a_pending_delivery_once(self):
        record = {"id": "will", "agent": "c-will", "incarnation_id": "b" * 32}
        record["execution"] = "supervised"
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        crew = Crew("will", record, self.current._replace(meta=meta))
        protocol.begin(crew, self.project, "Map the repo.")
        protocol.once(crew, "hooks_untrusted", "[codex-hooks-untrusted] Trust them once.")
        (pending,) = protocol.wait_all(crew.session, 0)["deliveries"]
        reply = self.call("ack_delivery", {"delivery_id": pending["delivery_id"]})
        self.assertEqual(reply, {"delivery_id": pending["delivery_id"], "state": "acknowledged"})
        self.assertEqual(protocol.wait_all(crew.session, 0)["deliveries"], [])
        with self.assertRaisesRegex(CaptainError, r"\[delivery-unknown\]"):
            self.call("ack_delivery", {"delivery_id": pending["delivery_id"]})

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

    def test_a_prompt_to_a_busy_captain_is_queued_and_sent_once_it_is_idle(self):
        self.call("start_captain", {"provider": "fake"})
        self.state["value"] = "busy"
        reply = self.call("message_captain", {"text": "Plan it.", "message_id": "m1"})
        self.assertEqual(
            reply,
            {"message_id": "m1", "delivery_id": "user-m1", "state": "queued", "landed": False},
        )
        self.assertEqual(self.rung, [])
        self.state["value"] = "idle"
        self.until(lambda: self.rung, "the queued prompt")
        self.assertEqual(self.rung, [("user-m1", "Plan it.")])
        prompts = [(a["message"], a["state"]) for a in self.actions() if a["kind"] == "prompt"]
        self.assertEqual(prompts, [("m1", "queued"), ("m1", "delivered")])
        self.assertEqual(self.actions()[-2]["text"], "Plan it.")

    def test_a_ring_that_fails_returns_a_typed_state_not_an_error(self):
        self.call("start_captain", {"provider": "fake"})
        self.state["value"] = "broken"
        reply = self.call("message_captain", {"text": "Go.", "message_id": "m1"})
        self.assertEqual((reply["state"], reply["landed"]), ("uncertain", False))
        self.assertEqual(reply["error"]["code"], "jsonrpc-error")
        listed = self.call("uncertain_deliveries")["deliveries"]
        self.assertEqual([row["delivery_id"] for row in listed], ["user-m1"])

    def test_answering_an_unknown_request_is_unknown_and_a_captain_token_refusal_is_not_a_token_error(
        self,
    ):
        self.call("start_captain", {"provider": "fake"})
        with self.assertRaisesRegex(CaptainError, r"\[captain-request-unknown\]"):
            self.call("answer_captain", {"request": "nope", "decision": "approve"})
        owner = supervisor.Supervisor(self.current, "token", threading.Event())
        error = supervisor.Handler.answer(owner, "command", {"token": owner.captain_token})
        self.assertEqual(
            (error["error"]["code"], error["error"]["data"]["code"]),
            (supervisor.REFUSED, "supervisor-captain-token"),
        )

    def test_a_retried_message_id_is_sent_once(self):
        self.call("start_captain", {"provider": "fake"})
        for _ in range(2):
            reply = self.call("message_captain", {"text": "Go.", "message_id": "m1"})
            self.assertEqual((reply["state"], reply["landed"]), ("delivered", True))
        self.assertEqual(self.rung, [("user-m1", "Go.")])
        with self.assertRaisesRegex(CaptainError, r"\[captain-message-id\]"):
            self.call("message_captain", {"text": "Stop.", "message_id": "m1"})
        with self.assertRaisesRegex(CaptainError, r"\[captain-message-id\]"):
            self.call("message_captain", {"text": "Go.", "message_id": "../m"})

    def test_an_unconfirmed_prompt_is_listed_until_the_user_resolves_it(self):
        alerts, publish = [], supervisor.publish

        def spy(owner, method, params):
            if method == "attention":
                alerts.append(params)
            publish(owner, method, params)

        self.enterContext(patch.object(supervisor, "publish", side_effect=spy))
        self.call("start_captain", {"provider": "fake"})
        self.state["value"] = "unconfirmed"
        reply = self.call("message_captain", {"text": "Go.", "message_id": "m1"})
        self.assertEqual(reply["state"], "uncertain")
        self.state["value"] = "idle"
        self.assertEqual(
            self.call("message_captain", {"text": "Go.", "message_id": "m1"})["state"], "uncertain"
        )
        alert = self.until(
            lambda: next((a for a in alerts if a.get("delivery_id")), None), "the alert"
        )
        self.assertEqual(
            (alert["code"], alert["delivery_id"]), ("captain-delivery-uncertain", "user-m1")
        )
        (listed,) = self.call("uncertain_deliveries")["deliveries"]
        self.assertEqual(listed["delivery_id"], "user-m1")
        self.assertEqual(len(self.rung), 1)
        self.call("resolve_captain_delivery", {"delivery_id": "user-m1", "decision": "resend"})
        self.until(lambda: len(self.rung) == 2, "the resent prompt")
        self.assertEqual(self.rung, [("user-m1", "Go.")] * 2)
        self.assertEqual(self.call("uncertain_deliveries"), {"deliveries": []})
        self.assertEqual(
            self.call("message_captain", {"text": "Go.", "message_id": "m1"})["state"], "delivered"
        )

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


class SupervisedLifecycleTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)
        record = {"id": "will", "agent": "c-will", "incarnation_id": "b" * 32}
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = {**record, "execution": "supervised"}
        self.owner = supervisor.Supervisor(self.current, "token", threading.Event())

    def test_an_interrupt_and_a_model_change_are_lifecycle_actions(self):
        adapter = fake_captain([], {"value": "busy"}, {})
        link = supervisor.Link("will", "ref", adapter, self.current.events("will"))
        link.ready.set()
        self.owner.links["will"] = link
        supervisor.interrupt(self.owner, {"crew": "will", "reason": "wrong file"})
        supervisor.set_model(self.owner, {"crew": "will", "model": "m2"})
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        self.assertEqual(
            [(a["crew"], a["state"], a["detail"]) for a in actions if a["kind"] == "lifecycle"],
            [("will", "interrupted", "wrong file"), ("will", "model-changed", "m2 (now)")],
        )

    def test_an_app_command_runs_without_the_captain_role(self):
        seen = {}

        def spawn(argv, **options):
            seen.update(options["env"])
            raise OSError("stopped here")

        with (
            patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}),
            patch.object(supervisor.subprocess, "Popen", side_effect=spawn),
            self.assertRaisesRegex(CaptainError, r"\[command-failed\]"),
        ):
            supervisor.command(self.owner, {"argv": ["status", "--json"]})
        self.assertNotIn("CAPTAIN_ROLE", seen)
        self.assertEqual(seen["CAPTAIN_SESSION"], self.current.meta["id"])

    def test_dashboard_runs_from_the_app_only_as_one_frame_or_json(self):
        for argv in (["dashboard"], ["dashboard", "--interval", "1"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(CaptainError, "command-refused"):
                supervisor.command(self.owner, {"argv": argv})


class CliJsonTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))

    def test_app_verbs_take_json_and_providers_needs_no_session(self):
        parser = cli.parser()
        for argv in (
            ["crew", "--task", "t", "--json"],
            ["tell", "will", "hi", "--json"],
            ["answer", "will", "--assignment", "a", "q", "yes", "--json"],
            ["interrupt", "will", "--json"],
            ["model", "will", "cheap", "--json"],
            ["dismiss", "will", "--json"],
            ["status", "--json"],
            ["dashboard", "--json"],
            ["providers", "--agent", "pi", "--json"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(parser.parse_args(argv).json)
        self.assertFalse(parser.parse_args(["status"]).json)
        rows = {
            "version": 1,
            "providers": [
                {"provider": "pi", "installed": True, "version": "0.9", "signed_in": False},
                {"provider": "codex", "installed": False, "version": None, "signed_in": None},
            ],
        }
        from captain_barbossa import provider_status

        for argv, printed in (
            (["providers", "--json"], json.dumps(rows) + "\n"),
            (["providers"], "pi: 0.9, signed out\ncodex: not installed\n"),
        ):
            output = io.StringIO()
            with (
                self.subTest(argv=argv),
                patch.object(provider_status, "catalogue", return_value=rows) as catalogue,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(output.getvalue(), printed)
                self.assertEqual(catalogue.call_args.args[1], None)

    def test_a_json_failure_is_an_envelope_on_stdout_and_text_stays_on_stderr(self):
        from captain_barbossa import provider_status

        failure = CaptainError("[provider-check] pi would not answer.")
        envelope = json.dumps({"error": {"code": "provider-check", "message": str(failure)}})
        for argv, out, err in (
            (["providers", "--json"], envelope + "\n", ""),
            (["providers"], "", f"captain: {failure}\n"),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(argv=argv),
                patch.object(provider_status, "catalogue", side_effect=failure),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(cli.main(argv), 1)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), (out, err))
        reply = supervisor.typed({"exit": 1, "stdout": envelope, "stderr": ""})
        self.assertEqual(reply["refusal"], {"code": "provider-check", "message": str(failure)})


class SuperviseCommandTests(unittest.TestCase):
    def test_supervise_takes_session_before_or_after_the_subcommand(self):
        parser = cli.parser()
        for argv in (["--session", "s1", "supervise"], ["supervise", "--session", "s1"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).session, "s1")
        with patch.dict(os.environ, {"CAPTAIN_SESSION": "from-env"}):
            self.assertEqual(cli.parser().parse_args(["supervise"]).session, "from-env")


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
