"""Plugin `hello` negotiation, registration and the published wire envelopes."""

import contextlib
import io
import tomllib
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import __version__, plugins
from tests.home_isolation import SessionCase
from tests.plugin_fixture import FakePlugin, clear_settings, golden, hello, register, unregister


class ProtocolNegotiationTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(unregister)
        self.plugin = FakePlugin(self.root)
        self.settings = Path.home() / ".captain" / "settings.toml"

    def test_requests_match_the_golden_envelopes(self):
        for name, request in (
            ("hello-request", plugins.request("hello", {})),
            (
                "sync-request",
                plugins.request("sync", {}, points={"context": 1}, server_id="api.example.com"),
            ),
        ):
            with self.subTest(name=name):
                expected = golden(name)
                expected["captain_version"] = __version__
                self.assertEqual(request, expected)

    def test_golden_responses_parse(self):
        found = plugins.negotiate("example", golden("hello-response"))
        self.assertIsNone(found.mismatch)
        self.assertEqual(found.versions, {"context": 1, "crew_denials": 1})
        self.assertEqual(
            found.denials,
            (("example", "proposals", "approve"), ("example_cli", "proposals", "approve")),
        )
        self.assertEqual(found.refresh, 900)
        files = plugins.validate("example", golden("sync-response"))
        self.assertEqual(
            [item["path"] for item in files["files"]], ["context/captain.md", "context/crew.md"]
        )
        self.assertEqual(plugins.validate("example", golden("sync-response-wipe"))["files"], [])
        with self.assertRaises(plugins.PluginError) as caught:
            plugins.checked(golden("error"))
        self.assertEqual(caught.exception.code, "login_required")

    def test_refresh_interval_defaults_to_900_seconds(self):
        self.assertEqual(plugins.negotiate("example", hello()).refresh, 900)
        self.assertEqual(plugins.negotiate("example", hello(refresh_interval=60)).refresh, 60)

    def test_mixed_versions_pick_the_highest_common_and_skip_optional_points(self):
        response = hello(context=(1, 2, 7))
        response["points"]["telemetry"] = {"versions": [3], "required": False}
        response["points"]["crew_denials"] = {
            "versions": [9],
            "required": False,
            "commands": [["example", "x"]],
        }
        found = plugins.negotiate("example", response)
        self.assertIsNone(found.mismatch)
        self.assertEqual(found.versions, {"context": 1})
        self.assertEqual(found.denials, ())

    def test_required_point_or_transport_without_a_common_version_is_a_mismatch(self):
        for response, needle in (
            (hello(context=(2,)), "context"),
            (hello(protocol=(2,)), "transport"),
        ):
            with self.subTest(needle=needle):
                found = plugins.negotiate("example", response)
                self.assertIn(needle, found.mismatch)
                self.assertNotIn("\n", found.mismatch)

    def test_optional_plugin_is_skipped_with_one_line_and_keeps_its_denials(self):
        register(self.plugin)
        self.plugin.script(hello=hello(context=(2,), commands=[("example", "approve")]))
        with self.assertRaises(plugins.PluginError) as caught:
            plugins.sync("example")
        self.assertEqual(caught.exception.code, "version_mismatch")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            binding = plugins.binding()
        self.assertEqual(binding["active"], [])
        self.assertEqual(binding["denials"], [["example", "approve"]])
        lines = error.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("example", lines[0])
        self.assertIn("skipped", lines[0])

    def test_required_plugin_blocks_launch_on_a_mismatch(self):
        register(self.plugin, required=("example",))
        self.plugin.script(hello=hello(context=(2,)))
        with self.assertRaises(plugins.PluginError):
            plugins.sync("example")
        with self.assertRaises(plugins.CaptainError) as caught:
            plugins.binding()
        self.assertIn("example", str(caught.exception))
        self.assertIn("context", str(caught.exception))

    def test_launch_renegotiates_from_the_cached_hello_without_a_subprocess(self):
        register(self.plugin)
        plugins.sync("example")
        before = len(self.plugin.requests())
        binding = plugins.binding()
        self.assertEqual(binding["active"], ["example"])
        self.assertEqual(len(self.plugin.requests()), before)

    def test_add_registers_then_syncs_and_remove_restores_the_file(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        original = '# mine\n[captain]\nagent = "claude"\n'
        self.settings.write_text(original, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            plugins.add("example", str(self.plugin.path), required=False)
        parsed = tomllib.loads(self.settings.read_text())
        self.assertEqual(parsed["captain"], {"agent": "claude"})
        self.assertEqual(
            parsed["plugins"]["example"], {"command": str(self.plugin.path), "required": False}
        )
        self.assertEqual([r["call"] for r in self.plugin.requests()], ["hello", "hello", "sync"])
        self.assertIsNotNone(plugins.pointer("example"))
        with contextlib.redirect_stdout(io.StringIO()):
            plugins.remove("example")
        self.assertEqual(self.settings.read_text(), original)

    def test_add_refuses_a_mismatch_and_writes_nothing(self):
        self.plugin.script(hello=hello(protocol=(2,)))
        with self.assertRaises(plugins.CaptainError):
            plugins.add("example", str(self.plugin.path), required=False)
        self.assertFalse(self.settings.exists())

    def test_add_refuses_bad_names_and_duplicates(self):
        for name in ("Example", "1x", "a" * 33, "a.b"):
            with self.subTest(name=name), self.assertRaises(plugins.CaptainError):
                plugins.add(name, str(self.plugin.path), required=False)
        with contextlib.redirect_stdout(io.StringIO()):
            plugins.add("example", str(self.plugin.path), required=False)
        with self.assertRaises(plugins.CaptainError):
            plugins.add("example", str(self.plugin.path), required=False)

    def test_malformed_plugin_settings_are_ignored_silently(self):
        from captain_barbossa import agents, sessions

        self.settings.parent.mkdir(parents=True, exist_ok=True)
        current = sessions.session(self.project, self.pane, create=True)
        cases = (
            'plugins = "oops"\n',
            "plugins = [1, 2]\n",
            '[plugins]\nexample = "not a table"\n',
            '[plugins.Bad_Name]\ncommand = "/bin/true"\n',
            "[plugins.example]\ncommand = 5\n",
            "[plugins.example]\nrequired = true\n",
            '[plugins.example]\ncommand = ""\n',
            '[plugins.example]\ncommand = "/no/such/plugin"\nrequired = "yes"\noptions = "x"\n',
            '[plugins.example]\ncommand = "/bin/true"\noptions = [1]\nrefresh = "soon"\n',
        )
        for text in cases:
            with self.subTest(text=text):
                self.settings.write_text(text, encoding="utf-8")
                clear_settings()
                with (
                    contextlib.chdir(self.project),
                    patch.object(plugins, "spawn"),
                    contextlib.redirect_stderr(io.StringIO()) as error,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    command, _ = agents.prepare_launch(
                        current, self.project, "claude", "/bin/claude", None
                    )
                    self.assertEqual(plugins.command(Namespace(plugin_command="list")), 0)
                    self.assertEqual(plugins.command(Namespace(plugin_command="status")), 0)
                self.assertEqual(error.getvalue(), "")
                self.assertNotIn("Plugin context", "\n".join(command))
                self.assertEqual(plugins.status("example")["error"], None)

    def test_add_reports_a_settings_file_it_cannot_extend(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text('plugins = "oops"\n', encoding="utf-8")
        with self.assertRaises(plugins.CaptainError) as caught:
            plugins.add("example", str(self.plugin.path), required=False)
        self.assertIn("by hand", str(caught.exception))
        self.assertEqual(self.settings.read_text(), 'plugins = "oops"\n')

    def test_repo_settings_never_register_a_plugin(self):
        repo = self.project / ".captain"
        repo.mkdir()
        (repo / "settings.toml").write_text(
            f'[plugins.example]\ncommand = "{self.plugin.path}"\n', encoding="utf-8"
        )
        with contextlib.chdir(self.project):
            unregister()
            self.assertEqual(plugins.registered(), {})
