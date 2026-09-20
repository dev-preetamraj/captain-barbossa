"""Settings come from .captain/settings.toml: project over home over the shipped
defaults.toml, one key at a time, and nothing is ever required."""

import contextlib
import io
import os
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import questionary

from captain_barbossa import agents, cli, config, dashboard, memory, models, runtime
from captain_barbossa.runtime import CaptainError

PANE = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        for path in (self.home, self.project):
            (path / ".captain").mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(config, "project_root", return_value=self.project))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def write(self, where, body):
        (where / ".captain" / "settings.toml").write_text(textwrap.dedent(body), encoding="utf-8")
        config.settings.cache_clear()

    def test_no_settings_anywhere_leaves_the_shipped_defaults(self):
        self.assertEqual(config.settings(), config.defaults())
        self.assertIsNone(config.text("captain", "model"))
        self.assertIs(config.flag("dashboard", "enabled"), False)
        for provider, tiers in models.TIERS.items():
            self.assertEqual(models.tiers_for(provider), tiers)

    def test_the_shipped_defaults_file_parses_and_carries_every_default(self):
        shipped = tomllib.loads(config.defaults_text())
        self.assertEqual(shipped, config.defaults())
        self.assertEqual(shipped["captain"]["model"], "")
        self.assertIs(shipped["dashboard"]["enabled"], False)
        self.assertEqual(set(shipped["models"]), {"claude", "codex"})
        for tiers in shipped["models"].values():
            self.assertEqual(set(tiers), set(models.TIER_NAMES))
        # models reads its table from the file, so the two cannot drift apart.
        self.assertEqual(models.TIERS, shipped["models"])

    def test_a_project_key_beats_a_global_key_beats_the_shipped_default(self):
        shipped = config.defaults()["models"]["claude"]
        self.assertEqual(models.tiers_for("claude")["cheap"], shipped["cheap"])
        self.write(self.home, '[models.claude]\ncheap = "sonnet"\n')
        self.assertEqual(models.tiers_for("claude")["cheap"], "claude-sonnet-5")
        self.write(self.project, '[models.claude]\ncheap = "opus"\n')
        self.assertEqual(models.tiers_for("claude")["cheap"], "claude-opus-5")
        # The two keys neither file sets still come from the shipped defaults.
        self.assertEqual(
            {tier: models.tiers_for("claude")[tier] for tier in ("mid", "strong")},
            {tier: shipped[tier] for tier in ("mid", "strong")},
        )

    def test_one_configured_tier_leaves_every_other_tier_at_its_default(self):
        self.write(
            self.project,
            """
            [models.claude]
            cheap = "sonnet"
        """,
        )
        self.assertEqual(
            models.tiers_for("claude"),
            {**models.TIERS["claude"], "cheap": "claude-sonnet-5"},
        )
        self.assertEqual(models.tiers_for("codex"), models.TIERS["codex"])

    def test_project_overrides_home_key_by_key_and_leaves_the_rest_falling_back(self):
        self.write(
            self.home,
            """
            [captain]
            model = "opus"

            [models.claude]
            cheap = "sonnet"
            mid = "opus"
        """,
        )
        self.write(
            self.project,
            """
            [models.claude]
            cheap = "fable"
        """,
        )
        self.assertEqual(
            models.tiers_for("claude"),
            {
                "cheap": "claude-fable-5-1",  # project wins
                "mid": "claude-opus-5",  # only home sets it
                "strong": models.TIERS["claude"]["strong"],  # neither file sets it
            },
        )
        # A project file that never mentions [captain] leaves the home setting standing.
        self.assertEqual(config.text("captain", "model"), "opus")

    def test_tier_values_may_be_aliases_ids_or_unknown_strings_kept_literal(self):
        self.write(
            self.project,
            """
            [models.claude]
            cheap = "Opus"
            mid = "claude-haiku-4-5"
            strong = "some-future-model"
        """,
        )
        self.assertEqual(
            models.tiers_for("claude"),
            {
                "cheap": "claude-opus-5",
                "mid": "claude-haiku-4-5",
                "strong": "some-future-model",
            },
        )
        # resolve_model reads tiers_for, so the overrides reach it without recursing.
        self.assertEqual(models.resolve_model("claude", "cheap"), "claude-opus-5")
        self.assertEqual(models.resolve_model("claude", "strong"), "some-future-model")

    def test_malformed_settings_raise_an_error_naming_the_file(self):
        path = self.project / ".captain" / "settings.toml"
        path.write_text("[captain\nmodel = ", encoding="utf-8")
        config.settings.cache_clear()
        with self.assertRaisesRegex(CaptainError, str(path)):
            config.settings()

    def test_malformed_settings_reach_the_user_through_main_not_a_traceback(self):
        """Settings are read by the command, so a bad file is an error, not a traceback."""
        path = self.project / ".captain" / "settings.toml"
        with patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.root / "state")}):
            current = memory.session(self.project, PANE, create=True)
            path.write_text("[captain\nmodel = ", encoding="utf-8")
            config.settings.cache_clear()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(stderr),
                patch.object(cli, "current_pane", lambda: PANE),
                patch.object(cli, "project_root", lambda: self.project),
            ):
                self.assertEqual(cli.main(["--session", current.meta["id"], "dashboard"]), 1)
        self.assertIn(str(path), stderr.getvalue())

    def test_version_and_help_never_read_settings(self):
        """--version must not parse a settings file, nor go looking for a project."""
        (self.project / ".captain" / "settings.toml").write_text(
            "[captain\nmodel = ", encoding="utf-8"
        )
        config.settings.cache_clear()
        with patch.object(config, "settings", side_effect=AssertionError("read settings")):
            for flag in ("--version", "--help"):
                with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_code:
                        cli.main([flag])
                    self.assertEqual(exit_code.exception.code, 0)


class LaunchTestCase(unittest.TestCase):
    """Launch a captain against a temp home and project whose settings the test writes."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        for path in (self.home, self.project):
            (path / ".captain").mkdir(parents=True)
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "CAPTAIN_STATE_ROOT": str(self.root / "state"),
                    "CAPTAIN_TEMP_ROOT": str(self.root / "temp"),
                },
                clear=True,
            )
        )
        self.enterContext(patch.object(config, "project_root", return_value=self.project))
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def launch(self, *flags, provider="claude"):
        """Run a captain launch; returns the exec'd command, with self.board the pane call."""
        agent = ["--agent", provider] if provider else []
        args = cli.parser().parse_args(["--session", self.meta["id"], *agent, *flags])
        with (
            patch.object(runtime, "herdr", return_value={}),
            patch.object(agents, "executable", side_effect=lambda name: f"/bin/{name}"),
            patch.object(agents.sys.stdin, "isatty", return_value=True),
            patch.object(agents, "start_dashboard", return_value="w1:p2") as board,
            patch.object(os, "execvpe") as execute,
        ):
            agents.launch(args, self.pane, self.project)
        self.board = board
        return execute.call_args.args[1]

    def write(self, body):
        (self.project / ".captain" / "settings.toml").write_text(
            textwrap.dedent(body), encoding="utf-8"
        )
        config.settings.cache_clear()


class CaptainModelTests(LaunchTestCase):
    """[captain] model picks the model the captain's own CLI launches with."""

    def test_configured_captain_model_becomes_the_native_model_flag(self):
        self.write("""
            [captain]
            model = "strong"
        """)
        command = self.launch()
        self.assertEqual(command[-2:], ["--model", "claude-opus-5"])
        self.write("""
            [captain]
            model = "terra"
        """)
        self.assertEqual(self.launch(provider="codex")[-2:], ["-m", "gpt-5.6-terra"])

    def test_captain_model_follows_a_configured_tier(self):
        self.write("""
            [captain]
            model = "cheap"

            [models.claude]
            cheap = "fable"
        """)
        self.assertEqual(self.launch()[-2:], ["--model", "claude-fable-5-1"])

    def test_no_captain_model_launches_the_cli_without_a_model_flag(self):
        command = self.launch()
        self.assertNotIn("--model", command)

    def test_an_unknown_captain_model_fails_the_launch_with_the_options(self):
        self.write("""
            [captain]
            model = "gigantic"
        """)
        with self.assertRaisesRegex(CaptainError, "No claude model matches 'gigantic'"):
            self.launch()


class DashboardSettingTests(LaunchTestCase):
    """[dashboard] enabled is opt-in, and --no-dashboard still overrules it."""

    def test_the_dashboard_pane_stays_shut_until_it_is_enabled(self):
        self.launch()
        self.board.assert_not_called()
        self.assertIsNone(memory.read_json(self.directory / "captain.json")["dashboard"])

    def test_enabling_it_opens_the_pane_and_records_it(self):
        self.write("""
            [dashboard]
            enabled = true
        """)
        self.launch()
        self.board.assert_called_once()
        self.assertEqual(memory.read_json(self.directory / "captain.json")["dashboard"], "w1:p2")

    def test_no_dashboard_beats_the_setting(self):
        self.write("""
            [dashboard]
            enabled = true
        """)
        self.launch("--no-dashboard")
        self.board.assert_not_called()

    def test_enabled_false_and_a_quoted_true_both_leave_it_shut(self):
        for body in ("enabled = false", 'enabled = "true"'):
            with self.subTest(body=body):
                self.write(f"[dashboard]\n{body}\n")
                self.launch()
                self.board.assert_not_called()


class CaptainAgentTests(LaunchTestCase):
    """[captain] agent skips the launch question; --agent still wins."""

    def test_a_configured_agent_launches_without_asking(self):
        self.write("""
            [captain]
            agent = "codex"
        """)
        with patch.object(questionary, "select", side_effect=AssertionError("asked a question")):
            self.assertEqual(self.launch(provider=None)[0], "/bin/codex")

    def test_the_flag_wins_over_the_setting(self):
        self.write("""
            [captain]
            agent = "codex"
        """)
        self.assertEqual(self.launch(provider="claude")[0], "/bin/claude")

    def test_an_agent_outside_providers_is_refused(self):
        self.write("""
            [captain]
            agent = "gpt"
        """)
        with self.assertRaisesRegex(CaptainError, r"\[captain\] agent is 'gpt'"):
            self.launch(provider=None)

    def test_the_refusal_names_the_file_that_set_it_not_always_the_project(self):
        path = self.home / ".captain" / "settings.toml"
        path.write_text('[captain]\nagent = "gpt"\n', encoding="utf-8")
        config.settings.cache_clear()
        with self.assertRaisesRegex(CaptainError, f"Fix it in {path}"):
            self.launch(provider=None)

    def test_no_setting_still_asks(self):
        with patch.object(agents, "choose", return_value="claude") as asked:
            self.launch(provider=None)
        self.assertEqual(asked.call_args.args[0], None)


class TunableTests(unittest.TestCase):
    """The settings with no launch of their own: read where they are used."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".captain").mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root / "nohome")}))
        self.enterContext(patch.object(config, "project_root", return_value=self.root))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def write(self, body):
        (self.root / ".captain" / "settings.toml").write_text(
            textwrap.dedent(body), encoding="utf-8"
        )
        config.settings.cache_clear()

    def test_the_shipped_defaults_are_the_ones_the_code_used_to_hardcode(self):
        self.assertEqual(config.text("crew", "model"), "cheap")
        self.assertEqual(config.lookup("crew", "wait_timeout", kind=int), 900)
        self.assertEqual(config.lookup("dashboard", "interval", kind=float), 2.0)
        self.assertEqual(agents.dashboard_ratio(), 0.8)

    def test_building_the_parser_resolves_no_setting_at_all(self):
        """Defaults belong to the command: a parser that reads them breaks --version."""
        self.write("""
            [crew]
            model = "strong"
            wait_timeout = 30
        """)
        with patch.object(config, "settings", side_effect=AssertionError("read settings")):
            args = cli.parser().parse_args(["crew", "--task", "x"])
            self.assertIsNone(args.model)
            self.assertIsNone(cli.parser().parse_args(["wait", "Jack"]).timeout)
            # A flag still overrules the file, and is the only thing the parser sets.
            self.assertEqual(
                cli.parser().parse_args(["wait", "Jack", "--timeout", "5"]).timeout, 5.0
            )
        self.assertEqual(config.text("crew", "model"), "strong")
        self.assertEqual(config.number("crew", "wait_timeout", kind=int), 30)

    def test_a_blank_crew_model_is_reported_not_an_attribute_error(self):
        self.write("""
            [crew]
            model = ""
        """)
        with self.assertRaisesRegex(CaptainError, "Provide a tier"):
            models.resolve_model("claude", config.text("crew", "model"))

    def test_a_number_outside_its_range_is_refused_and_names_the_file(self):
        path = self.root / ".captain" / "settings.toml"
        for body, names, kwargs in (
            ("[dashboard]\ninterval = -5\n", ("dashboard", "interval"), {}),
            ("[dashboard]\ninterval = 0\n", ("dashboard", "interval"), {}),
            ("[dashboard]\nratio = 1.5\n", ("dashboard", "ratio"), {"below": 1}),
            ("[dashboard]\nratio = 0\n", ("dashboard", "ratio"), {"below": 1}),
            ("[crew]\nwait_timeout = 0\n", ("crew", "wait_timeout"), {"kind": int}),
            ("[crew]\nwait_timeout = -1\n", ("crew", "wait_timeout"), {"kind": int}),
        ):
            with self.subTest(body=body):
                self.write(body)
                with self.assertRaisesRegex(CaptainError, f"must be a number.*{path}"):
                    config.number(*names, **kwargs)

    def test_an_out_of_range_interval_never_reaches_sleep(self):
        """time.sleep(-5) raises where the dashboard pane cannot report it."""
        self.write("""
            [dashboard]
            interval = -5
            ratio = 0
        """)
        with patch.object(dashboard.time, "sleep", side_effect=AssertionError("slept")):
            with self.assertRaisesRegex(CaptainError, r"\[dashboard\] interval"):
                dashboard.run(memory.Session(self.root, {"id": "x", "crew": {}}))
        with self.assertRaisesRegex(CaptainError, r"\[dashboard\] ratio"):
            agents.dashboard_ratio()

    def test_an_out_of_range_flag_is_refused_the_same_way_as_a_file(self):
        with patch.object(dashboard.time, "sleep", side_effect=AssertionError("slept")):
            with self.assertRaisesRegex(CaptainError, "--interval must be a number above 0"):
                dashboard.run(memory.Session(self.root, {"id": "x", "crew": {}}), -5)

    def test_interval_and_ratio_are_read_where_they_are_used(self):
        self.write("""
            [dashboard]
            interval = 10
            ratio = 0.5
        """)
        self.assertEqual(agents.dashboard_ratio(), 0.5)
        slept = []

        def stop(seconds):
            slept.append(seconds)
            raise KeyboardInterrupt

        with (
            patch.object(dashboard.time, "sleep", stop),
            patch.object(dashboard, "render", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            dashboard.run(memory.Session(self.root, {"id": "x", "crew": {}}))
        self.assertEqual(slept, [10.0])

    def test_a_wrong_type_leaves_the_shipped_default_standing(self):
        self.write("""
            [crew]
            wait_timeout = "900"

            [dashboard]
            interval = true
            ratio = "wide"
        """)
        self.assertEqual(config.lookup("crew", "wait_timeout", kind=int), 900)
        self.assertEqual(config.lookup("dashboard", "interval", kind=float), 2.0)
        self.assertEqual(agents.dashboard_ratio(), 0.8)


class InitTests(unittest.TestCase):
    """`captain init` writes a template that is inert until the user uncomments a line."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(config, "project_root", return_value=self.project))
        # init needs no Herdr pane, so main() must dispatch it before asking for one.
        self.enterContext(patch.object(cli, "current_pane", side_effect=AssertionError("pane")))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def run_init(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main(["init", *flags]), 0)
        config.settings.cache_clear()
        return printed.getvalue()

    def test_init_writes_a_project_template_that_changes_nothing(self):
        path = self.project / ".captain" / "settings.toml"
        self.assertIn(str(path), self.run_init())
        self.assertTrue(path.is_file())
        self.assertIsNone(config.text("captain", "model"))
        for provider, tiers in models.TIERS.items():
            self.assertEqual(models.tiers_for(provider), tiers)

    def test_a_second_init_leaves_the_existing_file_alone(self):
        path = self.project / ".captain" / "settings.toml"
        self.run_init()
        edited = '[captain]\nmodel = "opus"\n'
        path.write_text(edited, encoding="utf-8")
        self.assertIn(str(path), self.run_init())
        self.assertEqual(path.read_text(encoding="utf-8"), edited)
        self.assertEqual(config.text("captain", "model"), "opus")

    def test_global_init_targets_home_and_leaves_the_project_untouched(self):
        self.assertIn(str(self.home), self.run_init("--global"))
        self.assertTrue((self.home / ".captain" / "settings.toml").is_file())
        self.assertFalse((self.project / ".captain").exists())


if __name__ == "__main__":
    unittest.main()
