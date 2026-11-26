from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, config, model_catalogue, models, usage
from captain_barbossa.runtime import CaptainError


class ModelsJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(
            patch.object(config, "project_settings", return_value=self.root / "settings.toml")
        )
        self.enterContext(patch.object(usage, "_prices", return_value={}))
        self.enterContext(patch.object(config.Path, "home", return_value=self.root))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.native = model_catalogue.native_peer
        self.argv = []
        self.enterContext(
            patch.object(model_catalogue.runtime, "executable", side_effect=lambda name: name)
        )
        self.enterContext(
            patch.object(model_catalogue, "native_peer", side_effect=self.fake_native)
        )

    def fake_native(self, argv: list[str], root: Path, factory: object) -> object:
        self.argv.append(argv)
        return self.native([sys.executable, __file__, "--fake-native", argv[0]], root, factory)

    def provider(self, name: str) -> dict:
        return model_catalogue.catalogue(self.root, name)["providers"][0]

    def test_cli_json_without_session_or_herdr_returns_versioned_catalogue(self) -> None:
        output = io.StringIO()
        with (
            patch.object(cli, "active_project", return_value=self.root),
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            patch.object(cli, "read_session", side_effect=AssertionError("session read")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["models", "--json"]), 0)
        document = json.loads(output.getvalue())
        self.assertEqual(document["version"], 1)
        self.assertEqual(
            [row["provider"] for row in document["providers"]], ["codex", "claude", "pi"]
        )
        self.assertTrue(all(row["error"] is None for row in document["providers"]))
        self.assertTrue(
            all(model["name"] for provider in document["providers"] for model in provider["models"])
        )

    def test_claude_alias_moves_but_exact_old_id_and_context_remain(self) -> None:
        rows = {row["id"]: row for row in self.provider("claude")["models"]}
        self.assertEqual(rows["claude-fable-5-2"]["aliases"], ["fable"])
        self.assertEqual(rows["claude-fable-5-1"]["aliases"], [])
        self.assertEqual(rows["claude-fable-5-2"]["name"], "Claude Fable 5.2")
        self.assertNotIn("legacy", rows["claude-fable-5-2"])
        self.assertTrue(rows["claude-fable-5-1"]["legacy"])
        self.assertNotIn("legacy", rows["claude-haiku-5-5"])
        self.assertNotIn("legacy", rows["claude-sonnet-5-5"])
        self.assertNotIn("legacy", rows["claude-opus-5-5"])
        self.assertEqual(models.resolve_model("claude", "fable"), "claude-fable-5-2")
        for identifier in ("claude-fable-5-1", "claude-fable-5-2"):
            self.assertEqual(models.resolve_model("claude", identifier), identifier)
            self.assertEqual(rows[identifier]["context_window"], 1_000_000)
            self.assertEqual(usage.context_limit(identifier), 1_000_000)
            self.assertEqual(
                rows[identifier]["effort_levels"], ["low", "medium", "high", "xhigh", "max"]
            )
        self.assertEqual(self.argv, [])

    def test_tiers_use_layered_config_aliases_and_keep_unknown_ids(self) -> None:
        (self.root / ".captain").mkdir()
        (self.root / ".captain/settings.toml").write_text(
            '[models.claude]\ncheap = "sonnet"\nmid = "fable"\n'
        )
        (self.root / "settings.toml").write_text(
            '[models.claude]\ncheap = "fable"\nstrong = "future-model"\n'
        )
        result = self.provider("claude")
        self.assertEqual(
            result["tiers"],
            {"cheap": "claude-fable-5-2", "mid": "claude-fable-5-2", "strong": "future-model"},
        )
        future = next(row for row in result["models"] if row["id"] == "future-model")
        self.assertEqual(future["name"], "future-model")
        self.assertIsNone(future["context_window"])
        self.assertIsNone(future["effort_levels"])

    def test_codex_pages_native_efforts_without_inventing_context_or_dropping_aliases(self) -> None:
        result = self.provider("codex")
        self.assertIsNone(result["error"])
        rows = {row["id"]: row for row in result["models"]}
        self.assertEqual(rows["gpt-6.2-astra"]["aliases"], ["astra"])
        self.assertEqual(rows["gpt-6.2-astra"]["name"], "GPT-6.2-Astra")
        self.assertEqual(rows["gpt-6.2-astra"]["effort_levels"], ["low", "ultra"])
        self.assertIsNone(rows["gpt-6.2-astra"]["context_window"])
        self.assertNotIn("legacy", rows["gpt-6.2-astra"])
        self.assertTrue(rows["gpt-6-astra"]["legacy"])
        self.assertNotIn("legacy", rows["gpt-6.2-sol"])
        self.assertTrue(rows["gpt-6.1-sol"]["legacy"])
        self.assertNotIn("legacy", rows["gpt-6-luna"])
        self.assertTrue(rows["gpt-5.6-luna"]["legacy"])
        self.assertEqual(rows["native-new"]["name"], "Native New")
        self.assertTrue(rows["native-new"]["legacy"])
        self.assertEqual(rows["native-new"]["context_window"], 123_456)
        self.assertEqual(rows["native-new"]["effort_levels"], [])
        self.assertNotIn("gpt-reserve", rows)
        self.assertNotIn("codex-auto-review", rows)
        self.assertIsNone(rows["gpt-5.6-luna"]["effort_levels"])
        self.assertEqual(self.argv, [["codex", "app-server"]])

    def test_native_failure_keeps_bundled_models_and_null_capabilities(self) -> None:
        with patch.dict(model_catalogue.DISCOVER, {"codex": lambda root: self.fail_probe()}):
            result = self.provider("codex")
        self.assertEqual(result["error"], "not installed")
        self.assertEqual(len(result["models"]), len(models.MODELS["codex"]))
        self.assertTrue(all(row["effort_levels"] is None for row in result["models"]))
        rows = {row["id"]: row for row in result["models"]}
        self.assertTrue(rows["gpt-5.6-sol"]["legacy"])
        self.assertNotIn("legacy", rows["gpt-6.2-sol"])
        self.assertEqual(result["tiers"], models.TIERS["codex"])

    def fail_probe(self) -> None:
        raise CaptainError("not installed")

    def test_pi_queries_each_models_supported_levels_and_qualifies_ambiguous_ids(self) -> None:
        result = self.provider("pi")
        self.assertIsNone(result["error"])
        rows = {row["id"]: row for row in result["models"]}
        self.assertEqual(rows["first/shared"]["aliases"], [])
        self.assertEqual(rows["second/shared"]["aliases"], [])
        self.assertEqual(rows["local/tiny"]["aliases"], ["tiny"])
        self.assertEqual(rows["first/shared"]["name"], "Shared First")
        self.assertEqual(rows["second/shared"]["name"], "second/shared")
        self.assertEqual(rows["anthropic/claude-opus-5"]["name"], "Claude Opus 5")
        self.assertTrue(rows["anthropic/claude-opus-4-8"]["legacy"])
        self.assertNotIn("legacy", rows["anthropic/claude-opus-5"])
        self.assertEqual(rows["first/shared"]["effort_levels"], ["off", "high", "max"])
        self.assertEqual(rows["second/shared"]["effort_levels"], ["off", "low"])
        self.assertEqual(rows["local/tiny"]["effort_levels"], ["off"])
        self.assertEqual(rows["first/shared"]["context_window"], 1_000_000)
        self.assertIsNone(rows["local/tiny"]["context_window"])
        self.assertEqual(result["tiers"], {"cheap": None, "mid": None, "strong": None})
        for option in ("--no-session", "--no-extensions", "--no-mcp", "--no-tools", "--offline"):
            self.assertIn(option, self.argv[0])

    def test_partial_pi_tier_config_does_not_hide_models(self) -> None:
        (self.root / "settings.toml").write_text('[models.pi]\ncheap = "tiny"\n')
        result = self.provider("pi")
        self.assertEqual(result["tiers"], {"cheap": "local/tiny", "mid": None, "strong": None})
        self.assertEqual(len(result["models"]), 5)

    def test_pi_failed_model_switch_does_not_reuse_previous_models_levels(self) -> None:
        os.environ["CATALOGUE_TEST_MODE"] = "switch-fails"
        rows = {row["id"]: row for row in self.provider("pi")["models"]}
        self.assertIsNone(rows["second/shared"]["effort_levels"])
        self.assertEqual(rows["local/tiny"]["effort_levels"], ["off"])

    def test_malformed_native_metadata_stays_unknown(self) -> None:
        os.environ["CATALOGUE_TEST_MODE"] = "bad-metadata"
        codex = {row["id"]: row for row in self.provider("codex")["models"]}
        self.assertIsNone(codex["native-new"]["effort_levels"])
        self.assertIsNone(codex["native-new"]["context_window"])
        pi = {row["id"]: row for row in self.provider("pi")["models"]}
        self.assertIsNone(pi["local/tiny"]["effort_levels"])

    def test_repeated_codex_cursor_is_bounded_and_reported(self) -> None:
        os.environ["CATALOGUE_TEST_MODE"] = "repeat-cursor"
        self.assertIn("cursor", self.provider("codex")["error"])

    def test_timed_out_native_process_is_reaped(self) -> None:
        os.environ["CATALOGUE_TEST_MODE"] = "timeout"
        os.environ["CATALOGUE_TEST_PID"] = str(self.root / "pid")
        start = time.monotonic()
        with patch.object(model_catalogue, "PROBE_TIMEOUT", 0.8):
            result = self.provider("codex")
        self.assertIn("timeout", result["error"])
        self.assertLess(time.monotonic() - start, 5)
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_grok_json_is_explicitly_out_of_scope(self) -> None:
        with self.assertRaisesRegex(CaptainError, "codex, claude and pi only"):
            self.provider("grok")
        self.assertEqual(self.argv, [])

    def test_text_models_does_not_import_discovery_or_native_adapters(self) -> None:
        script = (
            "import sys; from captain_barbossa import cli; "
            "assert cli.main(['models', '--agent', 'claude']) == 0; "
            "assert not {'captain_barbossa.model_catalogue', 'captain_barbossa.pi_rpc', "
            "'captain_barbossa.codex_app_server', 'captain_barbossa.supervisor'} & sys.modules.keys()"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "claude:\n  models: claude-haiku-5-5 (haiku), claude-sonnet-5-5 (sonnet), "
            "claude-opus-5-5 (opus), claude-fable-5-1, claude-fable-5-2 (fable)\n"
            "  tiers: cheap=claude-haiku-5-5 mid=claude-sonnet-5-5 strong=claude-opus-5-5\n",
        )


@unittest.skipUnless(os.environ.get("CAPTAIN_LIVE_MODELS") == "1", "native model probes are opt-in")
class LiveModelsJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_installed_tools_return_metadata_without_sessions_or_model_turns(self) -> None:
        script = (
            "import sys; from unittest.mock import patch; "
            "from captain_barbossa import cli, runtime; "
            "patch.object(runtime, 'herdr', side_effect=AssertionError('Herdr called')).start(); "
            "patch.object(cli, 'read_session', side_effect=AssertionError('session read')).start(); "
            "assert cli.main(['models', '--json']) == 0; "
            "assert 'captain_barbossa.supervisor' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env={key: value for key, value in os.environ.items() if not key.startswith("CAPTAIN_")},
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["version"], 1)
        self.assertEqual(
            [row["provider"] for row in document["providers"]], ["codex", "claude", "pi"]
        )
        for provider in document["providers"]:
            with self.subTest(provider=provider["provider"]):
                self.assertIsNone(provider["error"])
                self.assertTrue(provider["models"])
                self.assertTrue(any(row["effort_levels"] for row in provider["models"]))


def fake_native(provider: str) -> None:
    mode = os.environ.get("CATALOGUE_TEST_MODE")
    if path := os.environ.get("CATALOGUE_TEST_PID"):
        Path(path).write_text(str(os.getpid()))
    selected = None
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method", message.get("type"))
        if method == "initialized":
            continue
        match method:
            case "initialize":
                data = {}
            case "model/list":
                if mode == "timeout":
                    time.sleep(10)
                if message["params"]["cursor"] is None:
                    data = {
                        "data": [
                            {
                                "id": "picker-id",
                                "model": "gpt-6.2-astra",
                                "displayName": "GPT-6.2-Astra",
                                "hidden": False,
                                "isDefault": True,
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low"},
                                    {"reasoningEffort": "ultra"},
                                ],
                            }
                        ],
                        "nextCursor": "page-two",
                    }
                else:
                    data = {
                        "data": [
                            {
                                "id": "native-new",
                                "displayName": 3 if mode == "bad-metadata" else "Native New",
                                "upgrade": "native-next",
                                "contextWindow": True if mode == "bad-metadata" else 123_456,
                                "supportedReasoningEfforts": [{}] if mode == "bad-metadata" else [],
                            },
                            {
                                "id": "gpt-reserve",
                                "displayName": "GPT Reserve",
                                "hidden": True,
                            },
                            {
                                "id": "codex-auto-review",
                                "displayName": "Auto Review",
                                "hidden": True,
                            },
                        ],
                        "nextCursor": "page-two" if mode == "repeat-cursor" else None,
                    }
            case "get_available_models":
                data = {
                    "models": [
                        {
                            "id": "shared",
                            "name": "Shared First",
                            "provider": "first",
                            "contextWindow": 1_000_000,
                        },
                        {"id": "shared", "provider": "second"},
                        {"id": "tiny", "name": "", "provider": "local", "contextWindow": -1},
                        {
                            "id": "claude-opus-4-8",
                            "name": "Claude Opus 4.8",
                            "provider": "anthropic",
                        },
                        {
                            "id": "claude-opus-5",
                            "name": "Claude Opus 5",
                            "provider": "anthropic",
                        },
                    ]
                }
            case "set_model":
                selected = message["provider"]
                if selected == "second" and mode == "switch-fails":
                    print(
                        json.dumps(
                            {
                                "id": message["id"],
                                "type": "response",
                                "command": method,
                                "success": False,
                                "error": "unavailable",
                            }
                        ),
                        flush=True,
                    )
                    continue
                data = {}
            case "get_available_thinking_levels":
                levels = {
                    "first": ["off", "high", "max"],
                    "second": ["off", "low"],
                    "local": ["off"],
                    "anthropic": ["off", "high"],
                }
                data = {"levels": [False] if mode == "bad-metadata" else levels[selected]}
            case _:
                raise AssertionError(f"Unexpected native operation: {method}")
        response = (
            {"id": message["id"], "result": data}
            if provider == "codex"
            else {
                "id": message["id"],
                "type": "response",
                "command": method,
                "success": True,
                "data": data,
            }
        )
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-native"]:
        fake_native(sys.argv[2])
    else:
        unittest.main()
