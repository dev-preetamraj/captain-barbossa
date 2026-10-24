"""Pane-free launch plans preserve native identity without launching a CLI."""

import contextlib
import io
import json
import os
from unittest.mock import patch

from captain_barbossa import agents, cli, models, sessions
from captain_barbossa.store import write_json
from tests.home_isolation import SessionCase


class LaunchPlanTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(cli, "project_root", return_value=self.project))
        self.enterContext(patch.object(agents, "executable", return_value="/bin/pi"))
        self.enterContext(patch.object(agents.config, "text", return_value=None))
        self.enterContext(patch.object(models, "models_for", return_value=[("provider/model", ())]))
        for module, name in (
            (cli, "bootstrap"),
            (cli, "current_pane"),
            (agents.runtime, "herdr"),
            (os, "execvpe"),
            (agents, "choose"),
            (agents, "close_panes_on_exit"),
        ):
            self.enterContext(patch.object(module, name, side_effect=AssertionError(name)))

    def plan(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["launch-plan", *args])
        return code, json.loads(output.getvalue())

    def test_new_plan_and_exact_resume(self):
        with patch.dict(
            os.environ,
            {"API_KEY": "secret", "CAPTAIN_CREW": "Jack", "CAPTAIN_SESSION": "inherited"},
        ):
            code, plan = self.plan("--agent", "pi", "--model", "provider/model", "--effort", "high")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["argv"][:3], ["/bin/pi", "--mode", "rpc"])
        self.assertIn("--append-system-prompt", plan["argv"])
        self.assertIn("--extension", plan["argv"])
        self.assertEqual(plan["argv"][-2:], ["--thinking", "high"])
        self.assertNotIn("--session", plan["argv"])
        self.assertIsNone(plan["nativeResumeIdentity"])
        self.assertNotIn("API_KEY", plan["env"])
        self.assertIsNone(plan["env"]["CAPTAIN_CREW"])
        self.assertEqual(plan["env"]["CAPTAIN_PROJECT"], str(self.project))
        current = sessions.session(self.project, None, plan["captainSessionId"])
        self.assertIsNone(current.meta["workspace"])
        native = self.root / "native.jsonl"
        native.touch()
        write_json(current.directory / "pi-session.json", {"sessionFile": str(native)})
        code, resumed = self.plan("--agent", "pi", "--session", current.meta["id"])
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["nativeResumeIdentity"], str(native))
        self.assertEqual(resumed["argv"][-2:], ["--session", str(native)])
        self.assertNotIn("--model", resumed["argv"])
        self.assertEqual(resumed["captainSessionId"], current.meta["id"])
        native.unlink()
        code, error = self.plan("--agent", "pi", "--session", current.meta["id"])
        self.assertEqual(code, 1)
        self.assertEqual(error["error"]["code"], "resume-unavailable")

    def test_model_tiers_are_resolved_in_shared_preparation(self):
        with patch.object(models, "tiers_for", return_value={"strong": "provider/model"}):
            code, result = self.plan("--agent", "pi", "--model", "strong")
            self.assertEqual(code, 0, result)
            self.assertEqual(result["argv"][result["argv"].index("--model") + 1], "provider/model")
            current = sessions.session(self.project, self.pane, create=True)
            command, _ = agents.prepare_launch(current, self.project, "pi", "/bin/pi", "strong")
            self.assertEqual(command[command.index("--model") + 1], "provider/model")

    def test_shared_overlay_pins_project_instead_of_inheriting_it(self):
        with patch.dict(os.environ, {"CAPTAIN_PROJECT": "/unrelated/project"}):
            code, result = self.plan("--agent", "pi", "--model", "provider/model")
        self.assertEqual(code, 0, result)
        current = sessions.session(self.project, self.pane, create=True)
        _, env = agents.prepare_launch(current, self.project, "pi", "/bin/pi", None)
        self.assertEqual(result["env"]["CAPTAIN_PROJECT"], str(self.project))
        self.assertEqual(env["CAPTAIN_PROJECT"], result["env"]["CAPTAIN_PROJECT"])

    def test_literal_message_is_not_a_launch_plan_subcommand(self):
        with (
            patch.dict(os.environ, {"CAPTAIN_SESSION": "inherited"}),
            patch.object(cli, "paneless_target", return_value=True),
            patch.object(cli, "tell_crew") as tell,
        ):
            self.assertEqual(cli.main(["tell", "Jack", "launch-plan"]), 0)
        self.assertEqual(tell.call_args.args[0].session, "inherited")
        for prefix in ([], ["--agent", "pi"], ["--session=explicit"], ["--no-dashboard"]):
            self.assertTrue(cli.is_launch_plan([*prefix, "launch-plan"]))
        self.assertFalse(cli.is_launch_plan(["--prompt", "launch-plan"]))

    def test_errors_are_one_json_object(self):
        for args, expected in (
            ([], "missing-choice"),
            (["--agent", "pi"], "missing-choice"),
            (["--agent", "claude"], "unsupported-transport"),
            (["--agent", "codex"], "unsupported-transport"),
            (["--agent", "grok"], "unsupported-transport"),
            (["--agent", "pi", "--session", "bad"], "invalid-session"),
            (["--model"], "invalid-arguments"),
        ):
            with self.subTest(args=args):
                code, result = self.plan(*args)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"]["code"], expected)

    def test_missing_identity_never_falls_back_to_new_chat(self):
        current = sessions.session(self.project, None, create=True)
        with sessions.crew_meta(current.directory) as meta:
            meta["captain_provider"] = "pi"
        code, result = self.plan("--agent", "pi", "--session", current.meta["id"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "resume-unavailable")

    def test_malformed_identity_and_provider_mismatch_fail_closed(self):
        current = sessions.session(self.project, None, create=True)
        code, result = self.plan("--agent", "pi", "--session", current.meta["id"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "session-provider-mismatch")
        with sessions.crew_meta(current.directory) as meta:
            meta["captain_provider"] = "pi"
        for identity in ([], {"sessionFile": "relative.jsonl"}, {"sessionFile": None}):
            write_json(current.directory / "pi-session.json", identity)
            code, result = self.plan("--agent", "pi", "--session", current.meta["id"])
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "resume-unavailable")

    def test_settings_supply_choices_and_failures_stay_json(self):
        with patch.object(agents.config, "text", side_effect=["pi", "provider/model"]):
            code, result = self.plan()
        self.assertEqual(code, 0, result)
        self.assertIn("provider/model", result["argv"])
        with patch.object(agents, "executable", side_effect=OSError("missing pi")):
            code, result = self.plan("--agent", "pi", "--model", "provider/model")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "launch-failed")

    def test_pane_sessions_still_check_workspace(self):
        current = sessions.session(self.project, self.pane, create=True)
        sessions.session(self.project, None, current.meta["id"])
        with self.assertRaisesRegex(agents.CaptainError, "workspace"):
            sessions.session(self.project, {"workspace_id": "other"}, current.meta["id"])
