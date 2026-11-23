"""With no plugin registered, captain imports, reads, runs and writes nothing new."""

import contextlib
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from captain_barbossa import agents, cli, config, events, launch, protocol, sessions, store
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase
from tests.plugin_fixture import clear_settings, unregister

PROVIDERS = ("claude", "codex", "pi", "grok")


class PluginsAbsentTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(unregister)
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)

    def launch(self, provider, enabled=True):
        """Launch with a fixed digest, since each launch also records itself in memory."""
        self.enterContext(patch.object(launch.recovery, "digest", return_value={"fixed": True}))
        command, env = launch.prepare_launch(
            self.current, self.project, provider, f"/bin/{provider}", None, plugins=enabled
        )
        # The pi captain extension gets a fresh temporary file name on every launch.
        return [arg for arg in command if not arg.endswith(".mjs")], env

    def imported(self, code):
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                code + "\nimport sys; print('captain_barbossa.plugins' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=self.project,
            env={**os.environ, "HOME": os.environ["HOME"]},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip().splitlines()[-1] == "True"

    def test_launch_is_identical_with_and_without_the_flag_and_touches_no_plugin_state(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider), patch.object(subprocess, "Popen") as popen:
                enabled = self.launch(provider)
                meta = store.read_json(self.current.meta_path)
                disabled = self.launch(provider, enabled=False)
                self.assertEqual(enabled, disabled)
                self.assertNotIn("Plugin context", "\n".join(enabled[0]))
                self.assertNotIn("plugins", meta)
                popen.assert_not_called()
        self.assertFalse((store.state_root() / "plugins").exists())

    def test_repo_only_plugin_table_is_ignored(self):
        repo = self.project / ".captain"
        repo.mkdir()
        (repo / "settings.toml").write_text(
            '[plugins.example]\ncommand = "/bin/false"\nrequired = true\n', encoding="utf-8"
        )
        clear_settings()
        baseline = self.launch("claude")
        self.assertNotIn("plugins", store.read_json(self.current.meta_path))
        (repo / "settings.toml").unlink()
        clear_settings()
        self.assertEqual(self.launch("claude"), baseline)

    def events(self):
        path = self.root / "unmanaged" / "events" / "x.jsonl"
        path.parent.mkdir(parents=True)
        return path

    def test_plugins_module_is_never_imported(self):
        self.assertFalse(self.imported("import captain_barbossa.cli"))
        self.assertFalse(
            self.imported(
                "from captain_barbossa import cli\n"
                "import contextlib, io\n"
                "with contextlib.redirect_stdout(io.StringIO()):\n"
                f"    cli.main(['hook', '{self.events()}', '{{}}'])"
            )
        )

    def test_crew_hook_without_a_binding_is_unchanged(self):
        crew = Crew(
            "jack", {"name": "Jack", "agent": "jack", "incarnation_id": "first"}, self.current
        )
        self.current.meta["crew"]["jack"] = crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(crew, self.project, "edit src", ["src"], ["edit"])
        crew.events.parent.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(
            events.plugin_denial(
                crew.events, {"tool_input": {"command": "example proposals approve"}}
            )
        )

    def test_crew_binding_is_a_no_op_without_a_session_binding(self):
        for provider in PROVIDERS:
            for path in ("foreground", "background"):
                with self.subTest(provider=provider, path=path):
                    meta = {}
                    record = {"id": "jack", "provider": provider}
                    self.assertEqual(
                        agents.bind_plugins(meta, record, provider, path, self.current.directory),
                        "",
                    )
                    self.assertEqual(record, {"id": "jack", "provider": provider})

    def test_recover_output_has_no_plugins_key(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
        self.assertNotIn("plugins", json.loads(output.getvalue()))

    def test_home_settings_are_read_once_and_shared_with_settings(self):
        clear_settings()
        with patch.object(config, "read", wraps=config.read) as read:
            config.settings()
            config.home_settings()
            config.settings()
        self.assertEqual(read.call_count, 2)
        self.assertIs(config.home_settings(), config.home_settings())
