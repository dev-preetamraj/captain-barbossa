"""Codex captains preallow only their own CLI and remember approvals only on request."""

import shlex
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from home_isolation import SessionCase

from captain_barbossa import codex_app_server, instructions
from captain_barbossa.runtime import CaptainError


class CodexCaptainApprovalTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_id = "captain-session"
        self.command = instructions.captain_command(self.session_id)
        self.link = SimpleNamespace(
            captain=True,
            crew_id="captain",
            env={"CAPTAIN_SESSION": self.session_id},
            root=str(self.project),
            thread="native-thread",
            model=None,
            native={},
            guard=threading.Lock(),
            peer=Mock(),
            relay=Mock(return_value=(0, None)),
            stdin=None,
            stdout=None,
        )

    def answer(self, command: object, method: str = "commandExecution") -> str:
        codex_app_server.answer(
            self.link,
            {
                "id": "approval-1",
                "method": f"item/{method}/requestApproval",
                "params": {"command": command, "itemId": "item-1", "turnId": "turn-1"},
            },
        )
        return self.link.peer.respond.call_args.args[1]["decision"]

    def verdict(self, behavior: str, remember: object = False) -> None:
        self.link.relay.return_value = (
            0,
            {"hookSpecificOutput": {"decision": {"behavior": behavior, "remember": remember}}},
        )

    def test_own_command_direct_or_shell_wrapped_accepts_without_relay(self) -> None:
        command = f"{self.command} --rename 'Review approvals'"
        for text in (command, shlex.join(["/bin/zsh", "-lc", command])):
            with self.subTest(command=text):
                self.assertEqual(self.answer(text), "accept")
                self.link.relay.assert_not_called()

    def test_unsafe_or_different_commands_reach_user(self) -> None:
        own = f"{self.command} status"
        other = instructions.captain_command("other-session")
        attempts = [
            f"{own} && touch /tmp/escaped",
            f"{own}; touch /tmp/escaped",
            f"{own} | tee /tmp/escaped",
            f"{own} &",
            f"{own}\ntouch /tmp/escaped",
            f"{own} > /tmp/escaped",
            f"{own} < /tmp/input",
            f"{self.command} --rename $(touch /tmp/escaped)",
            f"{self.command} --rename `touch /tmp/escaped`",
            f"{self.command} --rename <(touch /tmp/escaped)",
            f"{self.command} --rename $NAME",
            f"{self.command} --rename *",
            f"{self.command} --rename 'unterminated",
            f"{other} status",
            f"{self.command} --session other-session status",
            f"{self.command} --session=other-session status",
            f"{self.command} --sess other-session status",
            f"{self.command} --s=other-session status",
            own.replace(instructions.python_command(), "/tmp/python3", 1),
            f"env {own}",
            shlex.join(["/tmp/zsh", "-lc", own]),
            shlex.join(["/bin/zsh", "-lc", own, "extra"]),
            "echo hello",
        ]
        for text in attempts:
            for command in (text, shlex.join(["/bin/zsh", "-lc", text])):
                with self.subTest(command=command):
                    self.link.relay.reset_mock()
                    self.assertEqual(self.answer(command), "decline")
                    self.link.relay.assert_called_once()
                    self.assertEqual(
                        self.link.relay.call_args.args[0]["tool_input"], {"command": command}
                    )

    def test_missing_session_never_preallows(self) -> None:
        self.link.env = {}
        self.assertEqual(self.answer(f"{self.command} status"), "decline")
        self.link.relay.assert_called_once()

    def test_captain_decisions_remember_only_explicit_allow(self) -> None:
        for behavior, remember, expected in (
            ("allow", False, "accept"),
            ("allow", True, "acceptForSession"),
            ("allow", "yes", "accept"),
            ("deny", True, "decline"),
        ):
            with self.subTest(behavior=behavior, remember=remember):
                self.verdict(behavior, remember)
                self.assertEqual(self.answer("make test"), expected)
                self.assertEqual(self.link.native["approvals"], 0)

    def test_crew_commands_always_relay_and_never_remember(self) -> None:
        self.link.captain = False
        for remember in (False, True):
            with self.subTest(remember=remember):
                self.verdict("allow", remember)
                self.link.relay.reset_mock()
                self.assertEqual(self.answer(f"{self.command} status"), "accept")
                self.link.relay.assert_called_once()

    def test_file_changes_relay_and_remember_only_for_captain(self) -> None:
        self.link.native["items"] = {
            "item-1": {"changes": [{"kind": {"type": "add"}, "path": "/work/new.py"}]}
        }
        self.verdict("allow", True)
        for captain, expected in ((True, "acceptForSession"), (False, "accept")):
            with self.subTest(captain=captain):
                self.link.captain = captain
                self.link.relay.reset_mock()
                self.assertEqual(self.answer(None, "fileChange"), expected)
                self.link.relay.assert_called_once()

    def test_unavailable_or_malformed_approval_declines(self) -> None:
        for output in (None, {}, {"hookSpecificOutput": {"decision": "allow"}}):
            with self.subTest(output=output):
                self.link.relay.return_value = (0, output)
                self.assertEqual(self.answer("make test"), "decline")
        for error in (CaptainError("unavailable"), OSError("unavailable")):
            with self.subTest(error=error):
                self.link.relay.side_effect = error
                self.assertEqual(self.answer("make test"), "decline")
                self.assertEqual(self.link.native["approvals"], 0)

    def test_captain_start_and_resume_preserve_native_sandbox_and_exec_rules(self) -> None:
        for recorded in (None, "recorded-thread"):
            with self.subTest(recorded=recorded):
                self.link.thread = recorded
                peer = Mock()
                peer.request.return_value = {"thread": {"id": recorded or "new-thread"}}
                with patch.object(codex_app_server.jsonrpc, "Peer", return_value=peer):
                    codex_app_server.open(self.link)
                method, params, _ = peer.request.call_args.args
                expected = {"cwd": str(self.project)}
                if recorded:
                    expected.update(threadId=recorded, excludeTurns=True)
                self.assertEqual(method, "thread/resume" if recorded else "thread/start")
                self.assertEqual(params, expected)
                self.assertEqual(peer.request.call_count, 2)
