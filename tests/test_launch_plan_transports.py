"""launch-plan reads each captain's structured transport from models.TRANSPORTS."""

import contextlib
import io
import json
import os
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import agents, cli, instructions, launch, models, sessions


class LaunchPlanTransportTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(cli, "active_project", return_value=self.project))
        self.enterContext(
            patch.object(launch, "executable", side_effect=lambda provider: f"/bin/{provider}")
        )
        self.enterContext(patch.object(agents.config, "text", return_value=None))
        self.enterContext(patch.object(models, "models_for", return_value=[("provider/model", ())]))
        for module, name in (
            (cli, "bootstrap"),
            (cli, "current_pane"),
            (agents.runtime, "herdr"),
            (os, "execvpe"),
            (launch, "choose"),
            (launch, "close_panes_on_exit"),
        ):
            self.enterContext(patch.object(module, name, side_effect=AssertionError(name)))

    def plan(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["launch-plan", *args])
        return code, json.loads(output.getvalue())

    def test_each_provider_plans_its_own_structured_transport(self):
        for provider, transport, head in (
            ("codex", "app-server", ["/bin/codex", "app-server"]),
            ("claude", "sdk", instructions.native_command("/bin/claude", "sdk")),
            ("pi", "rpc", ["/bin/pi", "--mode", "rpc"]),
        ):
            with self.subTest(provider=provider):
                code, plan = self.plan("--agent", provider, "--model", "provider/model")
                self.assertEqual(code, 0, plan)
                self.assertEqual((plan["version"], plan["transport"]), (1, transport))
                self.assertEqual(plan["argv"][: len(head)], head)
                self.assertNotIn("notify=", " ".join(plan["argv"]))
                self.assertEqual(plan["argv"].count("--extension"), int(provider == "pi"))

    def test_a_provider_without_a_captain_transport_and_pi_only_flags_are_refused(self):
        for args, expected in (
            (["--agent", "grok", "--model", "m"], "unsupported-transport"),
            (["--agent", "codex", "--model", "m", "--effort", "high"], "invalid-arguments"),
        ):
            with self.subTest(args=args):
                code, result = self.plan(*args)
                self.assertEqual((code, result["error"]["code"]), (1, expected))

    def test_a_codex_session_resumes_with_a_fresh_native_conversation(self):
        code, first = self.plan("--agent", "codex", "--model", "provider/model")
        self.assertEqual(code, 0, first)
        code, resumed = self.plan("--agent", "codex", "--session", first["captainSessionId"])
        self.assertEqual(code, 0, resumed)
        self.assertIsNone(resumed["nativeResumeIdentity"])
        self.assertEqual(resumed["captainSessionId"], first["captainSessionId"])
        code, wrong = self.plan("--agent", "pi", "--session", first["captainSessionId"])
        self.assertEqual(wrong["error"]["code"], "session-provider-mismatch")

    def test_a_supervised_captain_gets_no_pi_bridge_and_the_app_instructions(self):
        current = sessions.session(self.project, None, create=True)
        with sessions.crew_meta(current.directory) as meta:
            meta["transport"] = "supervisor"
        current = current._replace(meta=meta)
        command, _ = launch.prepare_launch(
            current, self.project, "pi", "/bin/pi", None, transport="rpc", bridge=False
        )
        self.assertNotIn("--extension", command)
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("session in an app.", prompt)
        self.assertNotIn("inside Herdr", prompt)
