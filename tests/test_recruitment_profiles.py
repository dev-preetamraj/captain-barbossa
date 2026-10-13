"""Explicit profiles layer choices without changing assignment authority."""

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, background, cli, config, profiles, protocol, runtime, sessions
from captain_barbossa.pane import Pane
from captain_barbossa.placement import Placement
from captain_barbossa.store import read_json
from tests.home_isolation import SessionCase


class RecruitmentProfilesTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.settings = {}
        self.real_settings = config.settings.__wrapped__
        self.real_source = config.source
        self.enterContext(
            patch.object(config, "settings", lambda: config.merge(config.defaults(), self.settings))
        )
        self.enterContext(patch.object(config, "source", return_value=None))
        self.enterContext(patch.object(agents, "executable", return_value="/bin/codex"))
        self.enterContext(patch.object(agents, "shell_ready_for_input", return_value=True))
        self.enterContext(patch.object(Pane, "wait_for_crew"))
        self.enterContext(patch.object(protocol, "ring"))
        self.api = self.enterContext(
            patch.object(
                runtime,
                "herdr",
                return_value={"root_pane": {"pane_id": "w1:p9"}, "tab_id": "w1:t9"},
            )
        )
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def args(self, *flags):
        return cli.parser().parse_args(
            ["--session", self.meta["id"], "crew", "--task", "review", *flags]
        )

    def recruit_with_stderr(self, *flags):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            agents.create_crew(self.args(*flags), self.pane, self.project)
        return json.loads(out.getvalue()), error.getvalue()

    def recruit(self, *flags):
        result, _ = self.recruit_with_stderr(*flags)
        return result

    def test_global_and_project_keys_merge_and_keep_their_sources(self):
        home = self.project / "home"
        global_path = home / config.SETTINGS_PATH
        project_path = self.project / config.SETTINGS_PATH
        global_path.parent.mkdir(parents=True)
        project_path.parent.mkdir(parents=True)
        global_path.write_text('[profiles.review]\nagent="codex"\nmodel="cheap"\n')
        project_path.write_text('[profiles.review]\nmodel="strong"\nplacement="tab"\n')
        with (
            patch.object(Path, "home", return_value=home),
            patch.object(config, "project_settings", return_value=project_path),
            patch.object(config, "settings", self.real_settings),
            patch.object(config, "source", self.real_source),
        ):
            args, sources = profiles.apply(self.args("--profile", "review"))
        self.assertEqual((args.crew_agent, args.model, args.placement), ("codex", "strong", "tab"))
        self.assertIn(str(global_path), sources["agent"])
        self.assertIn(str(project_path), sources["model"])

    def test_flags_override_profile_and_models_resolve_for_effective_agent(self):
        self.settings = {
            "profiles": {
                "review": {
                    "agent": "claude",
                    "model": "cheap",
                    "execution": "background",
                    "placement": "tab",
                }
            }
        }
        result = self.recruit(
            "--profile", "review", "--agent", "codex", "--model", "strong", "--foreground"
        )
        self.assertEqual(result["profile"], "review")
        self.assertEqual(result["effective"]["agent"], "codex")
        self.assertEqual(result["model"], config.defaults()["models"]["codex"]["strong"])
        self.assertEqual(result["sources"]["agent"], "--agent")
        self.assertEqual(result["sources"]["execution"], "--foreground")
        self.assertIn("profiles.review", result["sources"]["placement"])
        saved = read_json(self.directory / "session.json")["crew"][result["id"]]
        for key in ("profile", "effective", "sources"):
            self.assertEqual(result[key], saved[key])

    def test_profile_tier_uses_flag_agent_and_explicit_placement_wins(self):
        self.settings = {
            "profiles": {"review": {"agent": "claude", "model": "mid", "placement": "pane"}}
        }
        result = self.recruit("--profile", "review", "--agent", "codex", "--placement", "tab")
        self.assertEqual(result["model"], config.defaults()["models"]["codex"]["mid"])
        self.assertIn("profiles.review", result["sources"]["model"])
        self.assertEqual(result["effective"]["placement"], "tab")
        self.assertEqual(result["sources"]["placement"], "--placement")

    def test_placement_auto_flag_overrides_profile_through_recruitment(self):
        self.settings = {"profiles": {"review": {"agent": "codex", "placement": "tab"}}}

        def herdr(*command, **_kwargs):
            if command[:2] == ("pane", "split"):
                return {"pane": {"pane_id": "w1:p10", "tab_id": self.pane["tab_id"]}}
            return {"root_pane": {"pane_id": "w1:p9"}, "tab_id": "w1:t9"}

        self.api.side_effect = herdr
        result, printed = self.recruit_with_stderr("--profile", "review", "--placement", "auto")
        self.assertEqual(result["effective"]["placement"], "pane")
        self.assertEqual(result["sources"]["placement"], "--placement")
        self.assertIn('"placement": "--placement"', printed)
        saved = read_json(self.directory / "session.json")["crew"][result["id"]]
        for key in ("profile", "effective", "sources"):
            self.assertEqual(result[key], saved[key])
        self.assertEqual(saved["sources"]["placement"], "--placement")

    def test_no_profile_short_placement_flag_recruits_with_flag_provenance(self):
        result, printed = self.recruit_with_stderr("--agent", "codex", "--p", "tab")
        self.assertEqual(result["placement"], "tab")
        self.assertIn('"placement": "--placement"', printed)
        saved = read_json(self.directory / "session.json")["crew"][result["id"]]
        self.assertEqual(saved["placement"], result["placement"])
        self.assertNotIn("sources", saved)
        self.assertNotIn("profile", result)
        self.assertNotIn("profile", saved)

    def test_background_flag_overrides_profile_execution(self):
        self.settings = {"profiles": {"research": {"agent": "codex", "execution": "foreground"}}}
        with patch.object(background, "start"):
            result = self.recruit("--profile", "research", "--background")
        self.assertEqual(result["effective"]["execution"], "background")
        self.assertEqual(result["sources"]["execution"], "--background")
        self.api.assert_not_called()

    def test_background_profile_persists_choices_and_cannot_grant_permissions(self):
        self.settings = {"profiles": {"research": {"agent": "codex", "execution": "background"}}}
        with patch.object(background, "start"):
            result = self.recruit("--profile", "research", "--allow", "read")
        self.api.assert_not_called()
        self.assertEqual(result["execution"], "background")
        self.assertEqual(result["effective"]["model"], result["model"])
        self.assertIn("profiles.research", result["sources"]["execution"])
        saved = read_json(self.directory / "session.json")["crew"][result["id"]]
        self.assertEqual(saved["sources"], result["sources"])
        self.assertNotIn("placement", result["effective"])

    def test_invalid_profiles_and_background_combinations_allocate_nothing(self):
        cases = [
            ({}, (), "Unknown recruitment profile"),
            ({"review": "codex"}, (), "must be a table"),
            ({"review": {"owns": ["src"]}}, (), "unknown key"),
            ({"review": {"allow": ["edit"]}}, (), "unknown key"),
            ({"review": {"agent": "wrong"}}, (), "invalid agent"),
            ({"review": {"model": 123}}, (), "nonempty string"),
            ({"review": {"execution": "daemon"}}, (), "invalid execution"),
            ({"review": {"placement": "window"}}, (), "invalid placement"),
            ({"review": {"agent": "codex", "model": "nonesuch"}}, (), "No codex model"),
            ({"review": {"agent": "claude", "execution": "background"}}, (), "background-provider"),
            (
                {"review": {"agent": "codex", "execution": "background", "placement": "tab"}},
                (),
                "background-flags",
            ),
            (
                {"review": {"agent": "codex", "execution": "background"}},
                ("--allow", "edit"),
                "background-read-only",
            ),
            (
                {"review": {"agent": "codex", "execution": "background"}},
                ("--owns", "src"),
                "background-flags",
            ),
        ]
        for table, flags, error in cases:
            self.settings = {"profiles": table}
            with (
                self.subTest(table=table, flags=flags),
                self.assertRaisesRegex(runtime.CaptainError, error),
            ):
                self.recruit("--profile", "review", *flags)
            self.assertEqual(read_json(self.directory / "session.json")["crew"], {})
        self.api.assert_not_called()

    def test_split_flags_override_profile_placement_and_omissions_keep_defaults(self):
        self.settings = {"profiles": {"review": {"agent": "codex", "placement": "tab"}}}
        args, sources = profiles.apply(self.args("--profile", "review", "--direction", "vertical"))
        rec = Placement(self.pane, sessions.Session(self.directory, self.meta)).resolve(
            args, self.directory
        )
        self.assertEqual(rec.placement, "pane")
        self.assertNotIn("placement", sources)
        self.assertEqual(rec.model, config.defaults()["models"]["codex"]["cheap"])

    def test_no_profile_does_not_read_profiles_or_change_arguments(self):
        self.settings = {"profiles": {"broken": 123}}
        args = self.args("--agent", "codex")
        resolved, sources = profiles.apply(args)
        self.assertIs(resolved, args)
        self.assertEqual(sources, {})
