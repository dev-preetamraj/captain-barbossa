"""Plugin context reaches launches in its own budget, honours expiry, and recover only reads."""

import contextlib
import io
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from captain_barbossa import agents, background, cli, launch, models, plugins, sessions, store
from tests.home_isolation import SessionCase
from tests.plugin_fixture import FakePlugin, register, snapshot, unregister

PROVIDERS = ("claude", "codex", "pi", "grok")


class ContextDeliveryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(unregister)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.plugin = FakePlugin(self.root)
        register(self.plugin)
        self.current = sessions.session(self.project, self.pane, create=True)
        self.popen = self.enterContext(patch.object(plugins, "spawn"))

    def launch(self, provider="claude", enabled=True):
        """The instruction text the native CLI receives, unescaped."""
        command, _ = launch.prepare_launch(
            self.current, self.project, provider, f"/bin/{provider}", None, plugins=enabled
        )
        if provider == "codex":
            value = next(arg for arg in command if arg.startswith("developer_instructions="))
            return json.loads(value.partition("=")[2])
        return command[command.index("--append-system-prompt") + 1]

    def section(self, text):
        return text[text.index("## Plugin context: example") :]

    def test_every_provider_gets_the_captain_section_after_the_cut_digest(self):
        plugins.sync("example")
        huge = {"preferences": [], "filler": "x" * 50_000}
        for provider in PROVIDERS:
            with (
                self.subTest(provider=provider),
                patch.object(launch.recovery, "digest", return_value=huge),
            ):
                text = self.launch(provider)
                self.assertIn("truncated; run CAPTAIN recover", text)
                self.assertIn("## Plugin context: example", text)
                self.assertIn("### context/captain.md\ncaptain context\n", text)
                self.assertNotIn("crew context", text)
                self.assertLess(text.index("truncated"), text.index("## Plugin context"))

    def test_section_bytes_are_raw_utf8_with_headings_and_the_ceiling_is_inclusive(self):
        empty = [{"path": "context/captain.md", "roles": ["captain"], "text": ""}]
        overhead = len(plugins.render("example", empty, "captain", 0, True).encode())
        room = plugins.PLUGIN_CEILING - overhead
        body = "é" * (room // 2) + "a" * (room % 2)
        self.plugin.script(sync=snapshot(captain=body))
        generation = plugins.sync("example")
        text = plugins.section("example", generation, "captain")
        self.assertEqual(len(text.encode("utf-8")), plugins.PLUGIN_CEILING)
        self.assertEqual(
            text,
            plugins.render(
                "example",
                [{**empty[0], "text": body}],
                "captain",
                plugins.manifest("example", generation)["synced_at"],
                False,
            ),
        )
        self.plugin.script(sync=snapshot(captain=body + "a"))
        with self.assertRaises(plugins.PluginError) as caught:
            plugins.sync("example")
        self.assertEqual(caught.exception.code, "context_too_large")

    def test_aggregate_ceiling_omits_later_plugins_whole_and_says_so(self):
        others = [FakePlugin(self.root, name) for name in ("beta", "gamma")]
        register(self.plugin, *others)
        body = "z" * (plugins.PLUGIN_CEILING - 200)
        for plugin in (self.plugin, *others):
            plugin.script(sync=snapshot(captain=body))
            plugins.sync(plugin.name)
        text = self.launch()
        self.assertIn("## Plugin context: example", text)
        self.assertIn("## Plugin context: beta", text)
        self.assertNotIn("## Plugin context: gamma", text)
        self.assertIn("[plugin gamma: context omitted", text)
        added = text[text.index("## Plugin context: example") :]
        self.assertLessEqual(len(added.encode()), plugins.TOTAL_CEILING)
        self.assertEqual(added.count(body), 2)

    def test_stale_generation_says_so_and_expired_one_injects_nothing(self):
        generation = plugins.sync("example")
        self.assertIn("(fresh).", self.section(self.launch()))
        manifest_path = plugins.directory("example") / "generations" / generation / "manifest.json"
        manifest = store.read_json(manifest_path)
        old = datetime.now(UTC) - timedelta(seconds=plugins.STALE_SECONDS + 60)
        store.write_json(manifest_path, {**manifest, "synced_at": old.timestamp()})
        self.assertIn("(stale).", self.section(self.launch()))
        self.plugin.script(sync=snapshot(days=-1))
        expired = plugins.sync("example")
        self.assertNotEqual(generation, expired)
        self.assertNotIn("Plugin context", self.launch())
        self.assertTrue(plugins.status("example")["expired"])

    def test_launch_starts_a_detached_refresh_only_when_due(self):
        plugins.sync("example")
        self.launch()
        self.popen.assert_not_called()
        pointer_path = plugins.directory("example") / "current.json"
        pointer = store.read_json(pointer_path)
        store.write_json(pointer_path, {**pointer, "synced_at": pointer["synced_at"] - 901})
        calls = len(self.plugin.requests())
        self.launch()
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertEqual(
            argv, [sys.executable, "-m", "captain_barbossa", "plugin", "sync", "example"]
        )
        self.assertEqual(len(self.plugin.requests()), calls)

    def test_never_synced_plugin_injects_nothing_and_refreshes(self):
        self.assertNotIn("Plugin context", self.launch())
        self.popen.assert_called_once()

    def test_no_plugins_flag_injects_nothing_and_binds_nothing(self):
        plugins.sync("example")
        self.assertNotIn("Plugin context", self.launch(enabled=False))
        self.assertNotIn("plugins", store.read_json(self.current.meta_path))
        self.popen.assert_not_called()
        args = cli.parser().parse_args(["--no-plugins"])
        self.assertTrue(args.no_plugins)

    def test_recover_reads_the_mirror_and_never_syncs_or_writes(self):
        plugins.sync("example")
        self.launch()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with (
            patch.object(plugins, "invoke", side_effect=AssertionError("plugin called")),
            patch.object(store, "write_json", side_effect=AssertionError("write")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
        digest = json.loads(output.getvalue())
        self.assertIn("captain context", digest["plugins"])
        self.popen.assert_not_called()
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_crew_gets_the_crew_section_from_the_generation_it_pinned(self):
        pinned = plugins.sync("example")
        self.launch()
        with agents.crew_meta(self.current.directory) as meta:
            record = {"id": "jack", "incarnation_id": "i1", "provider": "claude"}
            text = agents.bind_plugins(meta, record, "claude", "foreground", self.current.directory)
        self.assertIn("### context/crew.md\ncrew context\n", text)
        self.assertNotIn("captain context", text)
        self.assertEqual(record["plugins"]["generations"], {"example": pinned})
        self.plugin.script(sync=snapshot(crew="newer\n"))
        plugins.sync("example")
        generations = sorted(
            p.name for p in (plugins.directory("example") / "generations").iterdir()
        )
        self.assertIn(pinned, generations)
        self.assertIn("crew context", plugins.context(record["plugins"], "crew"))

    def test_background_crew_prompt_carries_the_crew_section(self):
        plugins.sync("example")
        self.launch()
        captured = {}

        def prepare(crew, project, provider, binary, model, task, assignment_id, context=""):
            captured["context"] = context
            raise store.CaptainError("stop here")

        args = cli.parser().parse_args(
            [
                "--session",
                self.current.meta["id"],
                "crew",
                "--background",
                "--agent",
                "pi",
                "--task",
                "look",
            ]
        )
        with (
            patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}),
            patch.object(agents, "executable", return_value="/bin/pi"),
            patch.object(models, "pi_models", return_value=[]),
            patch.object(background, "prepare", side_effect=prepare),
            patch.object(agents, "launch_choices", return_value=("pi", None, {})),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(store.CaptainError),
        ):
            agents.create_crew(args, self.pane, self.project)
        self.assertIn("### context/crew.md\ncrew context\n", captured["context"])
