import subprocess
import unittest
from unittest.mock import patch

from captain_barbossa import config, models, runtime
from captain_barbossa.runtime import CaptainError

PI_TABLE = """provider      model                context  max-out  thinking  images
ollama        llama3.2:3b          128K     16.4K    no        no
anthropic     claude-sonnet-5      200K     64K      yes       yes
openai-codex  gpt-5.4-mini         272K     128K     yes       yes
openai-codex  gpt-6-astra          272K     128K     yes       yes
bedrock       claude-sonnet-5      200K     64K      yes       yes
"""

# Verbatim `pi --list-models` from an install where the old capability-column ranking made
# ollama/llama3.2:3b the cheap tier and gemini-flash-lite-latest the strong one.
PI_LIVE = """provider      model                                    context  max-out  thinking  images
google        deep-research-max-preview-04-2026        131.1K   65.5K    yes       yes
google        deep-research-preview-04-2026            131.1K   65.5K    yes       yes
google        gemini-2.5-computer-use-preview-10-2025  128K     64K      yes       yes
google        gemini-2.5-flash                         1.0M     65.5K    yes       yes
google        gemini-2.5-flash-lite                    1.0M     65.5K    yes       yes
google        gemini-2.5-pro                           1.0M     65.5K    yes       yes
google        gemini-3-flash-preview                   1.0M     65.5K    yes       yes
google        gemini-3.1-flash-lite                    1.0M     65.5K    yes       yes
google        gemini-3.1-flash-lite-image              65.5K    4.1K     yes       yes
google        gemini-3.1-flash-lite-preview            1.0M     65.5K    yes       yes
google        gemini-3.1-flash-live-preview            131.1K   65.5K    yes       yes
google        gemini-3.1-pro-preview                   1.0M     65.5K    yes       yes
google        gemini-3.1-pro-preview-customtools       1.0M     65.5K    yes       yes
google        gemini-3.5-flash                         1.0M     65.5K    yes       yes
google        gemini-3.5-flash-lite                    1.0M     65.5K    yes       yes
google        gemini-3.6-flash                         1.0M     65.5K    yes       yes
google        gemini-3.7-flash                         1.0M     65.5K    yes       yes
google        gemini-3.8-flash                         1.0M     65.5K    yes       yes
google        gemini-flash-latest                      1.0M     65.5K    yes       yes
google        gemini-flash-lite-latest                 1.0M     65.5K    yes       yes
google        gemma-4-26b-a4b-it                       262.1K   32.8K    yes       yes
google        gemma-4-31b-it                           262.1K   32.8K    yes       yes
ollama        llama3.2:3b                              128K     16.4K    no        no
ollama        qwen3:4b                                 128K     16.4K    no        no
openai-codex  gpt-5.3-codex-spark                      128K     128K     yes       no
openai-codex  gpt-5.5                                  272K     128K     yes       yes
openai-codex  gpt-5.6-luna                             272K     128K     yes       yes
openai-codex  gpt-5.6-sol                              272K     128K     yes       yes
openai-codex  gpt-5.6-terra                            272K     128K     yes       yes
openai-codex  gpt-6-astra                              272K     128K     yes       yes
openai-codex  gpt-6-luna                               272K     128K     yes       yes
openai-codex  gpt-6-sol                                272K     128K     yes       yes
openai-codex  gpt-6.1-sol                              272K     128K     yes       yes
xai           grok-4.3                                 1M       30K      yes       yes
xai           grok-4.5                                 500K     500K     yes       yes
xai           grok-4.6                                 500K     500K     yes       yes
xai           grok-4.7                                 500K     500K     yes       yes
"""


def fake_pi(stdout=PI_TABLE, returncode=0, stderr=""):
    """Stand in for `pi --list-models` without a pi install."""
    models.pi_models.cache_clear()
    run = patch.object(
        runtime.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], returncode, stdout, stderr),
    )
    return patch.object(runtime, "executable", return_value="/usr/bin/pi"), run


class PiModelDiscoveryTests(unittest.TestCase):
    """pi is provider-agnostic, so its catalog comes from `pi --list-models`, not a table."""

    def tearDown(self):
        models.pi_models.cache_clear()

    def test_models_come_from_pi_with_provider_qualified_ids(self):
        which, run = fake_pi()
        with which, run as call:
            self.assertEqual(
                models.model_ids("pi"),
                [
                    "ollama/llama3.2:3b",
                    "anthropic/claude-sonnet-5",
                    "openai-codex/gpt-5.4-mini",
                    "openai-codex/gpt-6-astra",
                    "bedrock/claude-sonnet-5",
                ],
            )
            self.assertEqual(call.call_args.args[0], ["/usr/bin/pi", "--list-models"])

    def test_a_local_3b_model_never_becomes_a_pi_tier(self):
        which, run = fake_pi(PI_LIVE)
        with which, run:
            self.assertEqual(
                models.tiers_for("pi"),
                {
                    "cheap": "openai-codex/gpt-5.6-luna",
                    "mid": "openai-codex/gpt-5.6-sol",
                    "strong": "openai-codex/gpt-6-astra",
                },
            )
            self.assertEqual(models.resolve_model("pi", "cheap"), "openai-codex/gpt-5.6-luna")

    def test_pi_tiers_follow_the_first_family_models_ranks(self):
        claude = "anthropic  claude-haiku-5-5  200K  64K  yes  yes\n" + "\n".join(
            f"anthropic  {model}  200K  64K  yes  yes"
            for model in ("claude-sonnet-5-5", "claude-opus-5-5")
        )
        which, run = fake_pi(PI_LIVE + claude + "\n")
        with which, run:
            self.assertEqual(
                models.tiers_for("pi"),
                {tier: f"anthropic/{model}" for tier, model in models.TIERS["claude"].items()},
            )

    def test_a_gemini_only_catalog_ranks_lite_flash_pro_newest_first(self):
        google = "".join(
            line + "\n" for line in PI_LIVE.splitlines() if line.startswith(("provider", "google"))
        )
        which, run = fake_pi(google)
        with which, run:
            self.assertEqual(
                models.tiers_for("pi"),
                {
                    "cheap": "google/gemini-3.5-flash-lite",
                    "mid": "google/gemini-3.8-flash",
                    "strong": "google/gemini-3.1-pro-preview",
                },
            )

    def test_an_unrankable_catalog_fails_pointing_at_settings(self):
        local = "".join(
            line + "\n" for line in PI_LIVE.splitlines() if line.startswith(("provider", "ollama"))
        )
        which, run = fake_pi(local)
        with which, run:
            with self.assertRaisesRegex(CaptainError, r"cheap, mid, strong under \[models\.pi\]"):
                models.resolve_model("pi", "cheap")
            self.assertEqual(models.resolve_model("pi", "ollama/qwen3:4b"), "ollama/qwen3:4b")
            with patch.object(
                config,
                "text",
                side_effect=lambda *names: "qwen3:4b" if names[-1] == "cheap" else None,
            ):
                with self.assertRaisesRegex(CaptainError, r"Set mid, strong under"):
                    models.tiers_for("pi")

    def test_models_pi_settings_override_the_computed_tiers(self):
        which, run = fake_pi(PI_LIVE)
        pinned = {"cheap": "gemini-3.8-flash", "strong": "xai/grok-4.7"}
        with (
            which,
            run,
            patch.object(config, "text", side_effect=lambda *names: pinned.get(names[-1])),
        ):
            self.assertEqual(
                models.tiers_for("pi"),
                {
                    "cheap": "google/gemini-3.8-flash",
                    "mid": "openai-codex/gpt-5.6-sol",
                    "strong": "xai/grok-4.7",
                },
            )

    def test_unique_short_names_resolve_but_shared_ones_stay_ambiguous(self):
        which, run = fake_pi()
        with which, run:
            self.assertEqual(models.resolve_model("pi", "astra"), "openai-codex/gpt-6-astra")
            self.assertEqual(
                models.model_names("pi", "openai-codex/gpt-6-astra"),
                ("openai-codex/gpt-6-astra", "gpt-6-astra"),
            )
            with self.assertRaises(CaptainError) as error:
                models.resolve_model("pi", "claude-sonnet-5")
            self.assertIn("ambiguous", str(error.exception))

    def test_the_catalog_is_read_once_per_process(self):
        which, run = fake_pi()
        with which, run as call:
            models.resolve_model("pi", "astra")
            models.resolve_model("pi", "gpt-5.4-mini")
            models.model_ids("pi")
            self.assertEqual(call.call_count, 1)

    def test_a_failed_query_raises_instead_of_inventing_models(self):
        cases = (
            fake_pi(stdout="", returncode=1, stderr="pi: not authenticated"),
            fake_pi(stdout="provider  model  context  max-out  thinking  images\n"),
        )
        for which, run in cases:
            with self.subTest(), which, run, self.assertRaises(CaptainError) as error:
                models.model_ids("pi")
            self.assertIn("pi --list-models", str(error.exception))
        models.pi_models.cache_clear()
        with patch.object(runtime, "executable", side_effect=CaptainError("pi is not installed")):
            with self.assertRaises(CaptainError) as error:
                models.model_ids("pi")
        self.assertIn("pi --list-models", str(error.exception))


class ModelTierTests(unittest.TestCase):
    """Regression tests for the provider-neutral tier scheme (models.resolve_model)."""

    def test_every_tier_resolves_to_a_real_model_of_each_provider(self):
        for provider, tiers in models.TIERS.items():
            self.assertEqual(tuple(tiers), models.TIER_NAMES)
            for tier, model in tiers.items():
                with self.subTest(provider=provider, tier=tier):
                    self.assertIn(model, models.model_ids(provider))
                    self.assertEqual(models.resolve_model(provider, tier), model)

    def test_tiers_are_case_and_spacing_insensitive(self):
        self.assertEqual(models.resolve_model("claude", " Strong "), "claude-opus-5-5")
        self.assertEqual(models.resolve_model("codex", "CHEAP"), "gpt-5.6-luna")

    def test_tiers_are_ordered_cheapest_to_strongest_per_provider(self):
        for provider, tiers in models.TIERS.items():
            with self.subTest(provider=provider):
                order = models.model_ids(provider)
                picked = [order.index(tiers[tier]) for tier in models.TIER_NAMES]
                self.assertEqual(picked, sorted(picked))

    def test_free_text_matching_still_resolves_names_aliases_and_typos(self):
        self.assertEqual(models.resolve_model("claude", "opus"), "claude-opus-5-5")
        self.assertEqual(models.resolve_model("claude", "claude-haiku-5-5"), "claude-haiku-5-5")
        self.assertEqual(models.resolve_model("codex", "astra"), "gpt-6-astra")
        self.assertEqual(models.resolve_model("codex", "gpt 6.1 sol"), "gpt-6.1-sol")

    def test_unknown_text_lists_the_tiers_and_the_models(self):
        with self.assertRaises(CaptainError) as error:
            models.resolve_model("codex", "gigantic")
        message = str(error.exception)
        for tier in models.TIER_NAMES:
            self.assertIn(tier, message)
        self.assertIn("gpt-6-astra", message)

    def test_grok_tiers_and_labels_resolve_without_prefix_confusion(self):
        self.assertEqual(models.resolve_model("grok", "cheap"), "grok-4.7-build-fast")
        self.assertEqual(models.resolve_model("grok", "mid"), "grok-4.6")
        self.assertEqual(models.resolve_model("grok", " STRONG "), "grok-4.7")
        # The exact id wins over the longer build-fast id it is a prefix of.
        self.assertEqual(models.resolve_model("grok", "grok-4.7"), "grok-4.7")
        # Grok labels a model "Grok 4.6", the form its own switch confirmation echoes.
        self.assertEqual(models.model_names("grok", "grok-4.6"), ("grok-4.6", "grok 4.6"))

    def test_model_names_returns_the_id_with_its_aliases(self):
        self.assertEqual(
            models.model_names("claude", "claude-sonnet-5-5"), ("claude-sonnet-5-5", "sonnet")
        )
        self.assertEqual(models.model_names("codex", "gpt-6-sol"), ("gpt-6-sol",))

    def test_a_claude_id_labels_the_version_its_picker_lists_not_just_the_family(self):
        """A picker row is only the model asked for when the version matches: "opus"
        alone also names an "Opus 4.7" row, and that row once took the keypress."""
        labels = [models.claude_label(model) for model in models.model_ids("claude")]
        self.assertEqual(labels, ["haiku 5.5", "sonnet 5.5", "opus 5.5", "fable 5.1"])
        self.assertFalse("opus 4.7".startswith(models.claude_label("claude-opus-5-5")))
        self.assertTrue("opus 5.5.1".startswith(models.claude_label("claude-opus-5-5")))


class ModelTextTests(unittest.TestCase):
    def test_text_matches_exact_names_then_prefixes_substrings_and_close_spellings(self):
        for provider, text, expected in (
            ("claude", "claude-opus-5-5", "claude-opus-5-5"),
            ("claude", "opus", "claude-opus-5-5"),
            ("claude", "Claude Sonnet 5 5", "claude-sonnet-5-5"),
            ("claude", "haiku_5_5", "claude-haiku-5-5"),
            ("claude", "fable 5.1", "claude-fable-5-1"),
            ("claude", "sonet", "claude-sonnet-5-5"),
            ("codex", "gpt-6-sol", "gpt-6-sol"),
            ("codex", "astra", "gpt-6-astra"),
            ("codex", "gpt-6.1", "gpt-6.1-sol"),
            ("codex", ".6-sol", "gpt-5.6-sol"),
            ("codex", "terra", "gpt-5.6-terra"),
        ):
            with self.subTest(provider=provider, text=text):
                self.assertEqual(models.resolve_model(provider, text), expected)

    def test_unknown_and_ambiguous_text_list_the_provider_options(self):
        with self.assertRaisesRegex(CaptainError, "No claude model matches 'gpt-6-astra'"):
            models.resolve_model("claude", "gpt-6-astra")
        with self.assertRaisesRegex(CaptainError, "ambiguous for codex: gpt-5.6-luna"):
            models.resolve_model("codex", "gpt-5.6")
        for provider in models.MODELS:
            with self.subTest(provider=provider):
                with self.assertRaises(CaptainError) as error:
                    models.resolve_model(provider, "nonexistent-model-name")
                for model in models.model_ids(provider):
                    self.assertIn(model, str(error.exception))

    def test_a_near_miss_matching_several_models_is_unknown_not_ambiguous(self):
        """Three codex generations now share the "sol" name, so a typo close to all three
        must read as no real match, not as a choice between genuine candidates."""
        with self.assertRaisesRegex(CaptainError, "No codex model matches 'gpt-6-sel'") as error:
            models.resolve_model("codex", "gpt-6-sel")
        self.assertNotIn("ambiguous", str(error.exception))
        self.assertIn("gpt-6-sol", str(error.exception))

    def test_native_flags_follow_the_model_table(self):
        self.assertEqual(
            models.native_model_args("claude", "claude-opus-5"), ["--model", "claude-opus-5"]
        )
        self.assertEqual(models.native_model_args("codex", "gpt-6-astra"), ["-m", "gpt-6-astra"])
        self.assertEqual(models.native_model_args("codex", None), [])


if __name__ == "__main__":
    unittest.main()
