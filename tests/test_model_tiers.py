import unittest

from captain_barbossa import models
from captain_barbossa.runtime import CaptainError


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
        self.assertEqual(models.resolve_model("claude", " Strong "), "claude-opus-5")
        self.assertEqual(models.resolve_model("codex", "CHEAP"), "gpt-5.3-codex-spark")

    def test_tiers_are_ordered_cheapest_to_strongest_per_provider(self):
        for provider, tiers in models.TIERS.items():
            with self.subTest(provider=provider):
                order = models.model_ids(provider)
                picked = [order.index(tiers[tier]) for tier in models.TIER_NAMES]
                self.assertEqual(picked, sorted(picked))

    def test_free_text_matching_still_resolves_names_aliases_and_typos(self):
        self.assertEqual(models.resolve_model("claude", "opus"), "claude-opus-5")
        self.assertEqual(models.resolve_model("claude", "claude-haiku-4-5"), "claude-haiku-4-5")
        self.assertEqual(models.resolve_model("codex", "astra"), "gpt-6-astra")
        self.assertEqual(models.resolve_model("codex", "gpt 5.5"), "gpt-5.5")

    def test_unknown_text_lists_the_tiers_and_the_models(self):
        with self.assertRaises(CaptainError) as error:
            models.resolve_model("codex", "gigantic")
        message = str(error.exception)
        for tier in models.TIER_NAMES:
            self.assertIn(tier, message)
        self.assertIn("gpt-6-astra", message)

    def test_model_names_returns_the_id_with_its_aliases(self):
        self.assertEqual(
            models.model_names("claude", "claude-sonnet-5"), ("claude-sonnet-5", "sonnet")
        )
        self.assertEqual(models.model_names("codex", "gpt-5.5"), ("gpt-5.5",))


if __name__ == "__main__":
    unittest.main()
