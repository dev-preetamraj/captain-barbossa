import contextlib
import io
import os
from unittest.mock import patch

from captain_barbossa import cli
from tests.home_isolation import SessionCase


class CrewVerbsFromOutsideCwdTests(SessionCase):
    def setUp(self):
        SessionCase.setUp(self)
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        self.enterContext(contextlib.chdir(self.elsewhere))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "will"}))
        self.enterContext(patch.object(cli, "current_pane", return_value=self.pane))

    def run_main(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(argv)
        return code, err.getvalue()

    def test_protocol_verbs_run_from_outside_the_project(self):
        extra = {"ask": ["q"], "done": ["--report", "r"], "check": ["read"], "inbox": []}
        for verb, rest in extra.items():
            with (
                self.subTest(verb=verb),
                patch.object(cli, "protocol_command") as protocol,
                patch.object(cli, "print_inbox") as inbox,
            ):
                code, err = self.run_main([verb, "will", *rest])
                self.assertNotIn("outside the active", err)
                self.assertEqual(code, 0)
                self.assertTrue(protocol.called or inbox.called)

    def test_crew_verbs_never_reach_herdr_for_a_pane(self):
        # A sandboxed Codex crew cannot reach the Herdr socket; its verbs must not need it.
        extra = {"ask": ["q"], "done": ["--report", "r"], "check": ["read"], "inbox": []}
        unreachable = AssertionError("herdr pane current")
        with patch.object(cli, "current_pane", side_effect=unreachable):
            for verb, rest in extra.items():
                with (
                    self.subTest(verb=verb),
                    patch.object(cli, "protocol_command") as protocol,
                    patch.object(cli, "print_inbox") as inbox,
                ):
                    self.assertEqual(self.run_main([verb, "will", *rest])[0], 0)
                    called = protocol if verb != "inbox" else inbox
                    self.assertIsNone(called.call_args.args[1])
            with contextlib.chdir(self.project), patch.object(cli, "memory") as memory:
                argv = ["memory", "add", "will", "found", "x", "--scope", "session"]
                self.assertEqual(self.run_main(argv)[0], 0)
            self.assertIsNone(memory.call_args.args[1])

    def test_other_commands_keep_the_guard(self):
        code, err = self.run_main(["memory", "show"])
        self.assertNotEqual(code, 0)
        self.assertIn("outside the active", err)
