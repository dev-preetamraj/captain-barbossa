"""A supervised Claude or pi captain's native prompts reach the app as captain requests: Claude's
permission and AskUserQuestion calls, pi's extension dialogs; crew prompts are never put to the user."""

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
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    captain_bridge,
    claude_agent,
    config,
    pi_rpc,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.runtime import CaptainError


class Peer:
    """Records what an adapter answers on its native connection."""

    def __init__(self):
        self.answers, self.sent = [], []

    def respond(self, identifier, result, timeout):
        self.answers.append((identifier, result))

    def send(self, message, timeout):
        self.sent.append(message)


def link(captain, ask=None, relay=None):
    return SimpleNamespace(
        crew_id="captain" if captain else "jack",
        captain=captain,
        root="/work",
        peer=Peer(),
        ask=ask or (lambda kind, params: None),
        relay=relay or (lambda event: (0, None)),
    )


def approve(decision):
    return lambda event: (0, {"hookSpecificOutput": {"decision": {"behavior": decision}}})


class ClaudeCaptainPromptTests(SessionCase):
    def permission(self, target, tool, inputs):
        message = {
            "id": "host-1",
            "method": "permission",
            "params": {"tool": tool, "input": inputs},
        }
        claude_agent.permission(target, message)
        ((identifier, result),) = target.peer.answers
        self.assertEqual(identifier, "host-1")
        return result

    def test_a_captain_tool_call_is_decided_by_the_user(self):
        relayed = []

        def relay(event):
            relayed.append(event)
            return approve("allow")(event)

        result = self.permission(link(True, relay=relay), "Bash", {"command": "make test"})
        self.assertEqual(result, {"behavior": "allow"})
        self.assertEqual(relayed[0]["tool_input"], {"command": "make test"})
        denied = self.permission(link(True, relay=approve("deny")), "Bash", {"command": "rm"})
        self.assertEqual(denied["behavior"], "deny")

    def test_an_always_allow_answer_reaches_the_host_with_the_rules_it_offered(self):
        relayed = []
        rules = [{"type": "addRules", "behavior": "allow"}]

        def relay(event):
            relayed.append(event)
            return 0, {"hookSpecificOutput": {"decision": {"behavior": "allow", "remember": True}}}

        def ask(target, params):
            message = {"id": "host-1", "method": "permission", "params": params}
            claude_agent.permission(target, message)
            return target.peer.answers[-1][1]

        params = {"tool": "Bash", "input": {"command": "make"}, "rules": rules}
        self.assertEqual(
            ask(link(True, relay=relay), params), {"behavior": "allow", "remember": True}
        )
        self.assertEqual(relayed[0]["permission_rules"], rules)
        self.assertEqual(
            ask(link(True, relay=relay), {**params, "rules": []}), {"behavior": "allow"}
        )

    def test_the_supervised_claude_captain_preallows_only_its_own_session_commands(self):
        from captain_barbossa import instructions, launch

        current = sessions.session(self.project, None, create=True)
        own = f"Bash({instructions.captain_command(current.meta['id'])} *)"
        for transport, allowed in (("sdk", [own]), ("tui", None)):
            with self.subTest(transport=transport):
                command, _ = launch.prepare_launch(
                    current, self.project, "claude", "/bin/claude", None, transport=transport
                )
                settings = json.loads(command[command.index("--settings") + 1])
                self.assertEqual(settings.get("permissions", {}).get("allow"), allowed)

    def test_ask_user_question_answers_are_keyed_by_question_text(self):
        asked = []

        def ask(kind, params):
            asked.append((kind, params))
            return {"Which branch?": {"answers": ["main"]}}

        question = {"question": "Which branch?", "header": "Branch", "options": [{"label": "main"}]}
        result = self.permission(link(True, ask=ask), "AskUserQuestion", {"questions": [question]})
        self.assertEqual(result, {"behavior": "allow", "answers": {"Which branch?": "main"}})
        kind, params = asked[0]
        self.assertEqual(kind, "question")
        self.assertEqual(params["questions"][0]["id"], "Which branch?")

    def test_an_unanswered_question_denies_and_a_crew_is_never_asked(self):
        unanswered = self.permission(
            link(True), "AskUserQuestion", {"questions": [{"question": "?"}]}
        )
        self.assertEqual(unanswered["behavior"], "deny")
        asked = []
        crew = link(False, ask=lambda kind, params: asked.append(kind), relay=approve("allow"))
        self.assertEqual(self.permission(crew, "Bash", {"command": "ls"})["behavior"], "deny")
        self.assertEqual(asked, [])

    def test_only_the_captain_host_registers_the_permission_callback(self):
        from captain_barbossa import claude_host

        sdk = SimpleNamespace(ClaudeAgentOptions=lambda **given: SimpleNamespace(given=given))
        asked = claude_host.options(sdk, ["/c"], "/w", can_use_tool=print)[0].given
        self.assertIs(asked["can_use_tool"], print)
        self.assertNotIn("can_use_tool", claude_host.options(sdk, ["/c"], "/w")[0].given)


class PiCaptainDialogTests(SessionCase):
    def dialog(self, target, **event):
        pi_rpc._ask_dialog(target, {"type": "extension_ui_request", "id": "ui-1", **event})
        (sent,) = target.peer.sent
        self.assertEqual((sent["type"], sent["id"]), ("extension_ui_response", "ui-1"))
        return {key: value for key, value in sent.items() if key not in ("type", "id")}

    def test_a_confirm_is_an_approval_for_the_user(self):
        asked = []

        def ask(kind, params):
            asked.append((kind, params))
            return "approve"

        reply = self.dialog(
            link(True, ask=ask), method="confirm", title="Clear?", message="All gone"
        )
        self.assertEqual(reply, {"confirmed": True})
        self.assertEqual(asked[0][0], "approval")
        self.assertEqual(asked[0][1]["command"], "Clear? - All gone")

    def test_a_select_returns_only_one_of_its_options(self):
        def answer(value):
            return lambda kind, params: {"value": {"answers": [value]}}

        options = ["Allow", "Block"]
        reply = self.dialog(link(True, ask=answer("Block")), method="select", options=options)
        self.assertEqual(reply, {"value": "Block"})
        other = self.dialog(link(True, ask=answer("Maybe")), method="select", options=options)
        self.assertEqual(other, {"cancelled": True})
        typed = self.dialog(link(True, ask=answer("free text")), method="input", title="Name?")
        self.assertEqual(typed, {"value": "free text"})

    def test_an_unanswered_dialog_is_cancelled_and_a_crew_dialog_never_asks(self):
        self.assertEqual(self.dialog(link(True), method="confirm"), {"cancelled": True})
        crew = link(False, ask=lambda kind, params: self.fail("a crew dialog asked the user"))
        pi_rpc.handle(
            crew, {"method": "extension_ui_request", "params": {"id": "u", "method": "input"}}
        )
        deadline = time.monotonic() + 5
        while not crew.peer.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(crew.peer.sent[0]["cancelled"], True)


NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


class CaptainApprovalRouteTests(SessionCase):
    """A captain adapter's `ask('approval')` opens an approval request the app answers."""

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
        holder = {}
        adapter = types.SimpleNamespace(
            open=lambda link: holder.update(link=link),
            state=lambda link: "idle",
            ring=lambda link, message_id, text: {"landed": True, "gate": None},
            interrupt=lambda link, reason: "idle",
            set_model=lambda link, model: {"model": model, "applies": "now"},
        )
        self.holder = holder
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": adapter}))
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

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def test_a_dialog_approval_is_answered_in_the_app(self):
        self.call("start_captain", {"provider": "fake"})
        captain = self.holder["link"]
        replies = []
        payload = {"tool": "confirm", "command": "Clear?", "cwd": "/w"}
        threading.Thread(
            target=lambda: replies.append(captain.ask("approval", payload)), daemon=True
        ).start()
        deadline = time.monotonic() + 10
        while not (rows := self.call("captain_requests")["requests"]):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        normal = {**payload, "input": {}, "rules": []}
        self.assertEqual((rows[0]["kind"], rows[0]["payload"]), ("approval", normal))
        self.assertEqual(self.call("captain_state")["state"], "blocked")
        self.call(
            "answer_captain", {"request": rows[0]["id"], "decision": "approve", "remember": True}
        )
        while not replies:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(replies, ["approve"])  # pi keeps no rule: one approval
        self.assertEqual(self.call("captain_state")["state"], "idle")

    def test_an_always_allow_answer_offers_the_rules_and_returns_remember(self):
        self.call("start_captain", {"provider": "fake"})
        captain = self.holder["link"]
        rules = [{"type": "addRules", "behavior": "allow"}]
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "make"},
            "cwd": "/w",
            "permission_rules": rules,
        }
        replies = []
        threading.Thread(target=lambda: replies.append(captain.relay(event)), daemon=True).start()
        (row,) = self.until(lambda: self.call("captain_requests")["requests"], "the request")
        self.assertEqual(row["payload"]["rules"], rules)
        with self.assertRaisesRegex(CaptainError, r"\[captain-request-answer\]"):
            self.call(
                "answer_captain", {"request": row["id"], "decision": "approve", "remember": "yes"}
            )
        self.call("answer_captain", {"request": row["id"], "decision": "approve", "remember": True})
        (reply,) = self.until(lambda: replies, "the answer")
        decision = reply[1]["hookSpecificOutput"]["decision"]
        self.assertEqual(decision, {"behavior": "allow", "remember": True})

    def until(self, check, what):
        deadline = time.monotonic() + 10
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)
        return found

    def test_a_captain_turn_record_goes_to_its_hook_never_to_the_user(self):
        self.call("start_captain", {"provider": "fake"})
        captain = self.holder["link"]
        hooked = []
        with patch.object(supervisor, "relay_hook", side_effect=lambda link, e: hooked.append(e)):
            captain.relay({"type": "agent-turn-complete", "turn-id": "run-1"})
            captain.relay({"hook_event_name": "Notification", "notification_type": "idle_prompt"})
        self.assertEqual(len(hooked), 2)
        self.assertEqual(self.call("captain_requests")["requests"], [])

    def test_requests_no_captain_waits_on_expire_and_a_stop_declines_waiting_ones(self):
        directory = self.current.directory
        stale = captain_bridge.open_request(directory, "approval", {"tool": "Bash"})
        self.call("start_captain", {"provider": "fake"})
        states = {row["id"]: row["state"] for row in self.call("captain_requests")["requests"]}
        self.assertEqual(states, {stale["id"]: "expired"})
        captain = self.holder["link"]
        replies = []
        threading.Thread(
            target=lambda: replies.append(captain.ask("approval", {"tool": "Bash"})), daemon=True
        ).start()
        self.until(lambda: len(self.call("captain_requests")["requests"]) == 2, "the prompt")
        self.assertEqual(self.call("stop_captain"), {"outcome": "stopped"})
        self.assertEqual(self.until(lambda: replies, "the declined prompt"), [None])
        rows = self.call("captain_requests")["requests"]
        self.assertEqual([row["state"] for row in rows], ["expired", "expired"])

    def test_only_the_newest_closed_requests_are_kept(self):
        directory = self.current.directory
        for _ in range(4):
            row = captain_bridge.open_request(directory, "approval", {})
            captain_bridge.close_request(directory, row["id"], "answered", "deny")
        newest = captain_bridge.open_request(directory, "approval", {})
        with patch.object(supervisor, "REQUESTS_KEPT", 2):
            self.call("start_captain", {"provider": "fake"})
        rows = self.call("captain_requests")["requests"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["id"], newest["id"])


class CaptainRequestPayloadTests(unittest.TestCase):
    def test_questions_take_one_shape_whichever_provider_asked(self):
        codex = {
            "threadId": "t",
            "questions": [
                {
                    "id": "q1",
                    "header": "Pick",
                    "question": "Which?",
                    "isOther": True,
                    "isSecret": False,
                    "options": [{"label": "A", "description": "first"}],
                }
            ],
        }
        claude = {
            "questions": [
                {
                    "id": "Which?",
                    "question": "Which?",
                    "options": [{"label": "A"}],
                    "multiSelect": True,
                }
            ]
        }
        pi = {
            "method": "editor",
            "questions": [{"id": "value", "question": "Notes?", "prefill": "x"}],
        }
        keys = {"id", "header", "question", "options", "multi_select", "other", "secret"}
        keys |= {"multiline", "prefill"}
        for raw in (codex, claude, pi):
            (question,) = captain_bridge.request_payload("question", raw)["questions"]
            self.assertEqual(set(question), keys)
        shaped = captain_bridge.request_payload("question", codex)
        self.assertEqual(set(shaped), {"questions"})
        self.assertEqual(
            shaped["questions"][0]["options"], [{"label": "A", "description": "first"}]
        )
        self.assertTrue(shaped["questions"][0]["other"])
        claude_question = captain_bridge.request_payload("question", claude)["questions"][0]
        self.assertTrue(claude_question["multi_select"])
        pi_question = captain_bridge.request_payload("question", pi)["questions"][0]
        self.assertEqual((pi_question["multiline"], pi_question["prefill"]), (True, "x"))


class CodexCaptainSandboxTests(SessionCase):
    def test_a_codex_captain_keeps_the_users_own_sandbox_and_approvals(self):
        from captain_barbossa import codex_app_server

        started = []

        class Peer:
            def start(self):
                pass

            def notify(self, method, timeout):
                pass

            def request(self, method, params, timeout):
                started.append((method, params))
                return {"thread": {"id": "thread-1"}}

        link = SimpleNamespace(
            stdout=None, stdin=None, crew_id="captain", captain=True, root="/w", model="m"
        )
        with patch.object(codex_app_server.jsonrpc, "Peer", return_value=Peer()):
            codex_app_server.open(link)
        self.assertEqual(started[-1], ("thread/start", {"cwd": "/w", "model": "m"}))
        self.assertNotIn("hooks/list", [method for method, _ in started])
