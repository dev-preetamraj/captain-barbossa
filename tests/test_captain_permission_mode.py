"""The captain's permission mode: chosen at start, switched while it runs, recorded beside model and
effort, reported by captain_state, every change a lifecycle action; a plan approval keeps its plan
and a denial can carry the user's revision feedback."""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    claude_agent,
    claude_host,
    codex_app_server,
    config,
    eventlog,
    provider_status,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.runtime import CaptainError

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]
PLAN = {"plan": "# Plan\nCreate hello.txt.", "planFilePath": "/p/plan.md"}


def until(check, what):
    deadline = time.monotonic() + 10
    while not (found := check()):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)
    return found


class Recorder:
    def __init__(self, answers=None):
        self.answers, self.sent = answers or {}, []

    def request(self, method, params, timeout):
        self.sent.append((method, params))
        return self.answers.get(method, {})

    def notify(self, method, timeout):
        self.sent.append((method, None))

    def start(self):
        pass

    def respond(self, identifier, result, timeout):
        self.sent.append(("respond", result))


class SupervisorPermissionModeTests(SessionCase):
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
        self.opened, self.set = [], []

        def open_link(link):
            self.link = link
            self.opened.append(link.permission_mode)

        def set_permission_mode(link, mode, native):
            self.set.append((mode, native))
            link.permission_mode = mode
            return {"permission_mode": mode, "applies": "now"}

        fake = types.SimpleNamespace(
            open=open_link,
            state=lambda link: "idle",
            ring=lambda link, message_id, text: {"landed": True, "gate": None},
            interrupt=lambda link, reason: "idle",
            set_permission_mode=set_permission_mode,
            permission_mode=lambda link: link.permission_mode,
        )
        bare = types.SimpleNamespace(**{**vars(fake), "set_permission_mode": None})
        del bare.set_permission_mode
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": fake, "bare": bare}))
        self.enterContext(
            patch.dict(supervisor.PERMISSION_MODES, {"fake": provider_status.CLAUDE_MODES})
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

    def recorded(self):
        return store.read_json(self.current.directory / "captain.json").get("permission_mode")

    def changes(self):
        eventlog.flush_actions(self.current.directory)
        rows, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        return [
            (row["previous"], row["permission_mode"])
            for row in rows
            if row["kind"] == "lifecycle" and row["state"] == "permission-mode-changed"
        ]

    def test_a_start_mode_is_used_recorded_and_reused_by_the_same_provider(self):
        started = self.call("start_captain", {"provider": "fake", "permission_mode": "plan"})
        self.assertEqual((started["permission_mode"], self.opened), ("plan", ["plan"]))
        self.assertEqual(self.recorded(), "plan")
        self.assertEqual(self.call("captain_state")["permission_mode"], "plan")
        self.call("stop_captain")
        self.assertEqual(self.call("captain_state")["permission_mode"], "plan")
        self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.opened, ["plan", "plan"])
        self.assertEqual(self.changes(), [(None, "plan")])

    def test_a_switch_and_a_change_the_native_reports_are_each_one_lifecycle_action(self):
        self.call("start_captain", {"provider": "fake", "permission_mode": "plan"})
        reply = self.call("set_captain_permission_mode", {"mode": "acceptEdits"})
        self.assertEqual(reply, {"permission_mode": "acceptEdits", "applies": "now"})
        self.assertEqual(self.set, [("acceptEdits", None)])
        self.call("set_captain_permission_mode", {"mode": "acceptEdits"})
        self.link.permission_mode = "default"
        self.link.mode_changed()
        self.assertEqual(self.recorded(), "default")
        self.assertEqual(
            self.changes(), [(None, "plan"), ("plan", "acceptEdits"), ("acceptEdits", "default")]
        )

    def test_a_first_turn_on_the_users_settings_records_no_mode(self):
        self.call("start_captain", {"provider": "fake"})
        self.link.permission_mode = "default"
        self.link.mode_changed()
        self.assertEqual(self.call("captain_state")["permission_mode"], "default")
        self.assertEqual(self.changes(), [(None, "default")])
        self.assertIsNone(self.recorded())
        self.call("stop_captain")
        self.assertIsNone(self.call("captain_state")["permission_mode"])
        self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.opened, [None, None])

    def test_a_native_change_updates_an_existing_record(self):
        self.call("start_captain", {"provider": "fake", "permission_mode": "plan"})
        self.link.permission_mode = "acceptEdits"
        self.link.mode_changed()
        self.assertEqual(self.recorded(), "acceptEdits")
        self.assertEqual(self.changes(), [(None, "plan"), ("plan", "acceptEdits")])
        self.call("stop_captain")
        self.call("start_captain", {"provider": "fake"})
        self.assertEqual(self.opened, ["plan", "acceptEdits"])

    def test_a_native_change_without_a_record_creates_none(self):
        self.call("start_captain", {"provider": "fake"})
        for mode in ("plan", "acceptEdits"):
            self.link.permission_mode = mode
            self.link.mode_changed()
        self.assertEqual(self.call("captain_state")["permission_mode"], "acceptEdits")
        self.assertEqual(self.changes(), [(None, "plan"), ("plan", "acceptEdits")])
        self.assertIsNone(self.recorded())
        self.call("set_captain_permission_mode", {"mode": "default"})
        self.assertEqual(self.recorded(), "default")

    def test_unknown_modes_and_providers_without_modes_are_refused(self):
        with self.assertRaisesRegex(CaptainError, r"\[permission-mode-unsupported\]"):
            self.call("start_captain", {"provider": "fake", "permission_mode": "yolo"})
        self.call("start_captain", {"provider": "fake"})
        self.assertIsNone(self.call("captain_state")["permission_mode"])
        with self.assertRaisesRegex(CaptainError, r"\[permission-mode-unsupported\]"):
            self.call("set_captain_permission_mode", {"mode": "read-only"})
        self.call("stop_captain")
        self.call("start_captain", {"provider": "bare"})
        with self.assertRaisesRegex(CaptainError, r"\[permission-mode-unsupported\]"):
            self.call("set_captain_permission_mode", {"mode": "plan"})
        self.assertEqual(self.changes(), [])

    def relay(self, tool, inputs):
        replies = []
        event = {"hook_event_name": "PermissionRequest", "tool_name": tool, "tool_input": inputs}
        threading.Thread(target=lambda: replies.append(self.link.relay(event)), daemon=True).start()
        (row,) = until(lambda: self.call("captain_requests")["requests"], "the request")
        until(lambda: self.call("captain_state")["state"] == "blocked", "the waiting slot")
        return row, replies

    def test_a_plan_reaches_the_app_and_a_denial_carries_the_users_feedback(self):
        self.call("start_captain", {"provider": "fake", "permission_mode": "plan"})
        row, replies = self.relay("ExitPlanMode", PLAN)
        self.assertEqual((row["payload"]["tool"], row["payload"]["input"]), ("ExitPlanMode", PLAN))
        with self.assertRaisesRegex(CaptainError, r"\[captain-request-answer\]"):
            self.call(
                "answer_captain", {"request": row["id"], "decision": "approve", "message": "x"}
            )
        feedback = "Split the change into two commits."
        self.call("answer_captain", {"request": row["id"], "decision": "deny", "message": feedback})
        (reply,) = until(lambda: replies, "the answer")
        decision = reply[1]["hookSpecificOutput"]["decision"]
        self.assertEqual(decision, {"behavior": "deny", "message": feedback})
        protocol = store.read_json(self.current.directory / "protocol.json")
        (closed,) = protocol["captain_requests"]
        self.assertEqual((closed["answer"], closed["message"]), ("deny", feedback))

    def test_a_plain_denial_keeps_the_default_text(self):
        self.call("start_captain", {"provider": "fake"})
        row, replies = self.relay("Bash", {"command": "rm -rf build"})
        self.call("answer_captain", {"request": row["id"], "decision": "deny"})
        (reply,) = until(lambda: replies, "the answer")
        self.assertEqual(reply[1]["hookSpecificOutput"]["decision"], {"behavior": "deny"})


class ProviderRowTests(SessionCase):
    def test_an_unprobed_provider_row_has_null_permission_modes(self):
        owner = types.SimpleNamespace(
            lock=threading.Lock(), statuses={"pi": {"provider": "pi"}}, sign_ins={}
        )
        (row,) = supervisor.providers(owner, {"provider": "pi"})["providers"]
        self.assertIn("permission_modes", row)
        self.assertIsNone(row["permission_modes"])


def claude_link(peer, **fields):
    values = dict(
        peer=peer,
        native={},
        model=None,
        turn=None,
        guard=threading.Lock(),
        captain=True,
        root="/work",
        crew_id="captain",
        stdin=3,
        stdout=4,
        thread=None,
        effort=None,
        permission_mode=None,
        mode_changed=None,
    )
    return types.SimpleNamespace(**{**values, **fields})


class ClaudePermissionModeTests(SessionCase):
    def test_the_start_mode_goes_to_connect_and_a_switch_applies_now(self):
        peer = Recorder({"connect": {"session_id": "sid"}})
        link = claude_link(peer, permission_mode="plan")
        with patch.object(claude_agent.jsonrpc, "Peer", return_value=peer):
            claude_agent.open(link)
        self.assertEqual(peer.sent[0], ("connect", {"ask": True, "permission_mode": "plan"}))
        changed = claude_agent.set_permission_mode(link, "acceptEdits")
        self.assertEqual(changed, {"permission_mode": "acceptEdits", "applies": "now"})
        self.assertEqual(peer.sent[-1], ("set_permission_mode", {"mode": "acceptEdits"}))
        self.assertEqual(claude_agent.permission_mode(link), "acceptEdits")

    def test_init_and_status_messages_report_the_mode_and_a_change_is_signalled(self):
        changed = threading.Event()
        link = claude_link(Recorder(), mode_changed=changed.set)

        def system(subtype, mode):
            data = {"type": "SystemMessage", "subtype": subtype, "data": {"permissionMode": mode}}
            claude_agent.handle(
                link, {"method": "message", "params": {"kind": "SystemMessage", "data": data}}
            )

        system("init", "plan")
        self.assertTrue(changed.wait(5))
        changed.clear()
        system("init", "plan")
        self.assertFalse(changed.wait(0.2))
        system("status", "default")
        self.assertTrue(changed.wait(5))
        self.assertEqual(claude_agent.permission_mode(link), "default")

    def test_a_denial_message_reaches_the_host_in_place_of_the_default_text(self):
        def relay(event):
            verdict = {"behavior": "deny", "message": "Revise: smaller steps."}
            return 0, {"hookSpecificOutput": {"decision": verdict}}

        peer = Recorder()
        link = claude_link(peer, relay=relay)
        message = {"id": "h1", "method": "permission", "params": {"tool": "ExitPlanMode"}}
        claude_agent.permission(link, message)
        self.assertEqual(
            peer.sent[-1], ("respond", {"behavior": "deny", "message": "Revise: smaller steps."})
        )


class ClaudeHostPermissionModeTests(SessionCase):
    def test_options_take_the_mode_and_only_an_asking_host_may_enter_bypass(self):
        sdk = types.SimpleNamespace(ClaudeAgentOptions=lambda **given: given)

        async def ask(tool_name, tool_input, context):
            return None

        built, _ = claude_host.options(
            sdk, ["claude"], "/w", can_use_tool=ask, permission_mode="plan"
        )
        self.assertEqual(built["permission_mode"], "plan")
        self.assertIn("allow-dangerously-skip-permissions", built["extra_args"])
        crew, _ = claude_host.options(sdk, ["claude"], "/w")
        self.assertNotIn("permission_mode", crew)
        self.assertNotIn("allow-dangerously-skip-permissions", crew["extra_args"])

    def test_an_approved_plan_keeps_the_plan_in_the_updated_input(self):
        host = claude_host.Host(["claude"], 1)
        host.sdk = types.SimpleNamespace(
            PermissionResultAllow=lambda **given: types.SimpleNamespace(behavior="allow", **given),
            PermissionResultDeny=lambda **given: types.SimpleNamespace(behavior="deny", **given),
        )

        async def request(method, params, timeout):
            return {"result": {"behavior": "allow"}}

        host.request = request
        context = types.SimpleNamespace(suggestions=[])
        result = asyncio.run(host.ask("ExitPlanMode", dict(PLAN), context))
        self.assertEqual((result.behavior, result.updated_input), ("allow", PLAN))

    def test_a_switch_is_claudes_own_control_request_and_a_refusal_is_typed(self):
        host = claude_host.Host(["claude"], 1)
        modes = []

        async def set_permission_mode(mode):
            modes.append(mode)
            if mode == "auto":
                raise Exception("auto mode unavailable")

        host.client = types.SimpleNamespace(set_permission_mode=set_permission_mode)
        self.assertEqual(asyncio.run(host.call("set_permission_mode", {"mode": "plan"})), {})
        with self.assertRaisesRegex(CaptainError, r"^\[permission-mode-refused\]"):
            asyncio.run(host.call("set_permission_mode", {"mode": "auto"}))
        self.assertEqual(modes, ["plan", "auto"])


class CodexPermissionModeTests(SessionCase):
    def test_a_preset_opens_the_thread_rides_every_turn_and_a_switch_waits_for_the_next(self):
        preset = provider_status.CODEX_PRESETS["read-only"]
        peer = Recorder({"thread/start": {"thread": {"id": "t1"}}, "turn/start": {}})
        link = claude_link(peer, permission_mode="read-only", permission=preset, writable=())
        with patch.object(codex_app_server.jsonrpc, "Peer", return_value=peer):
            codex_app_server.open(link)
        started = dict(peer.sent)["thread/start"]
        self.assertEqual(
            (started["approvalPolicy"], started["sandbox"], started["approvalsReviewer"]),
            ("on-request", "read-only", "user"),
        )
        codex_app_server.ring(link, "m1", "hello")
        turn = dict(peer.sent)["turn/start"]
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly"})
        link.turn = "running"
        full = provider_status.CODEX_PRESETS["full-access"]
        changed = codex_app_server.set_permission_mode(link, "full-access", full)
        self.assertEqual(changed, {"permission_mode": "full-access", "applies": "next-turn"})
        link.turn = None
        codex_app_server.ring(link, "m2", "again")
        turn = [params for method, params in peer.sent if method == "turn/start"][-1]
        self.assertEqual(
            (turn["approvalPolicy"], turn["sandboxPolicy"], turn["approvalsReviewer"]),
            ("never", {"type": "dangerFullAccess"}, "user"),
        )
        self.assertEqual(codex_app_server.permission_mode(link), "full-access")

    def test_a_captain_without_a_preset_keeps_the_users_own_settings(self):
        peer = Recorder({"thread/start": {"thread": {"id": "t1"}}})
        link = claude_link(peer, permission=None, writable=())
        with patch.object(codex_app_server.jsonrpc, "Peer", return_value=peer):
            codex_app_server.open(link)
        started = dict(peer.sent)["thread/start"]
        self.assertFalse({"approvalPolicy", "sandbox", "approvalsReviewer"} & set(started))
