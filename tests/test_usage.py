import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import usage
from captain_barbossa.models import model_ids
from captain_barbossa.usage import context_limit, model_for_events, usage_for_events


def _jsonl(path, records):
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records),
        encoding="utf-8",
    )


def _stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _turn(model="claude-opus-5", call=None, when=None, hour=0, **counts):
    write = counts.get("cache_write", 0)
    message = {
        "model": model,
        "usage": {
            "input_tokens": counts.get("input", 0),
            "output_tokens": counts.get("output", 0),
            "cache_read_input_tokens": counts.get("cache_read", 0),
            "cache_creation_input_tokens": write,
            "cache_creation": {
                "ephemeral_1h_input_tokens": hour,
                "ephemeral_5m_input_tokens": write - hour,
            },
        },
    }
    if call is not None:
        message["id"] = call
    record = {"type": "assistant", "message": message}
    if when is not None:
        record["timestamp"] = when
    return record


# The LiteLLM extract for every id models.MODELS launches, read 2026-09-20. Tests point
# CAPTAIN_PRICES at this so none of them touches the state root or the network.
PRICES = {
    "claude-haiku-4-5": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 1e-07,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_creation_input_token_cost_above_1hr": 2e-06,
        "max_input_tokens": 200000,
    },
    "claude-sonnet-5": {
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 2e-07,
        "cache_creation_input_token_cost": 2.5e-06,
        "cache_creation_input_token_cost_above_1hr": 4e-06,
        "max_input_tokens": 1000000,
    },
    "claude-opus-5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_read_input_token_cost": 5e-07,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_creation_input_token_cost_above_1hr": 1e-05,
        "max_input_tokens": 1000000,
    },
    "claude-fable-5-1": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_read_input_token_cost": 2.5e-07,
        "cache_creation_input_token_cost": 1.25e-05,
        "cache_creation_input_token_cost_above_1hr": 2e-05,
        "max_input_tokens": 1000000,
    },
    "gpt-5.6-luna": {
        "input_cost_per_token": 2e-07,
        "output_cost_per_token": 1.2e-06,
        "cache_read_input_token_cost": 2e-08,
        "cache_creation_input_token_cost": 2.5e-07,
        "max_input_tokens": 922000,
    },
    "gpt-5.6-terra": {
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 1.2e-05,
        "cache_read_input_token_cost": 2e-07,
        "cache_creation_input_token_cost": 2.5e-06,
        "max_input_tokens": 922000,
    },
    "gpt-5.6-sol": {
        "input_cost_per_token": 4e-06,
        "output_cost_per_token": 2e-05,
        "cache_read_input_token_cost": 4e-07,
        "cache_creation_input_token_cost": 5e-06,
        "max_input_tokens": 922000,
    },
    "gpt-5.5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 5e-07,
        "max_input_tokens": 1050000,
    },
    "gpt-6-astra": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_read_input_token_cost": 1e-06,
        "cache_creation_input_token_cost": 1.25e-05,
        "max_input_tokens": 922000,
    },
}


class LimitTestCase(unittest.TestCase):
    """Pins the environment: no inherited context limit, and a fixed local price table."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {}, clear=False))
        os.environ.pop(usage.CONTEXT_LIMIT_ENV, None)
        self.prices = self.root / "prices.json"
        self.prices.write_text(json.dumps(PRICES), encoding="utf-8")
        os.environ[usage.PRICES_ENV] = str(self.prices)


class UsageTests(LimitTestCase):
    def setUp(self):
        super().setUp()
        self.events = self.root / "jack.jsonl"
        self.transcript = self.root / "transcript.jsonl"

    def point_at_transcript(self, extra=()):
        _jsonl(
            self.events,
            [
                {"hook_event_name": "PermissionRequest", "tool_name": "Bash"},
                *extra,
                {
                    "hook_event_name": "Stop",
                    "transcript_path": str(self.transcript),
                },
            ],
        )

    def test_sums_totals_and_reports_last_turn_context(self):
        self.point_at_transcript()
        _jsonl(
            self.transcript,
            [
                {"type": "user", "message": {"role": "user"}},
                _turn(input=10, output=200, cache_read=1000, cache_write=500),
                _turn(input=2, output=300, cache_read=1500, cache_write=40),
            ],
        )
        result = usage_for_events(self.events)
        self.assertEqual(
            {name: value for name, value in result.items() if name != "cost"},
            {
                "input": 12,
                "output": 500,
                "cache_read": 2500,
                "cache_write": 540,
                "tokens": 3552,
                # Input-only: the last turn's 2 + 1500 + 40, output excluded, which is
                # what Claude Code's own used_percentage counts.
                "context": 1542,
                "limit": 1_000_000,
                "model": "claude-opus-5",
                "rate": None,
            },
        )
        self.assertAlmostEqual(result["cost"], 0.017185)

    def test_accumulates_every_transcript_the_events_file_named(self):
        # /clear gives the crew a new session id and a new transcript; the spend before
        # it lives only in the earlier file and the total must never shrink.
        cleared = self.root / "cleared.jsonl"
        _jsonl(cleared, [_turn(input=9999)])
        self.point_at_transcript(
            extra=[
                {
                    "hook_event_name": "SessionStart",
                    "source": "clear",
                    "transcript_path": str(cleared),
                }
            ]
        )
        _jsonl(self.transcript, [_turn(input=7)])
        result = usage_for_events(self.events)
        self.assertEqual(result["input"], 10_006)
        # Context is the newest transcript's last turn, not the cumulative one.
        self.assertEqual(result["context"], 7)

    def test_a_transcript_named_twice_is_counted_once(self):
        self.point_at_transcript(
            extra=[{"hook_event_name": "SessionStart", "transcript_path": str(self.transcript)}]
        )
        _jsonl(self.transcript, [_turn(input=7)])
        self.assertEqual(usage_for_events(self.events)["input"], 7)

    def test_one_response_split_across_content_blocks_is_priced_once(self):
        # Claude Code writes an assistant record per content block (thinking, then each
        # tool_use) and every copy repeats the whole response's usage.
        self.point_at_transcript()
        _jsonl(
            self.transcript,
            [
                _turn(call="msg_1", input=4, output=100, cache_read=1000),
                _turn(call="msg_1", input=4, output=100, cache_read=1000),
                _turn(call="msg_2", input=4, output=50),
            ],
        )
        result = usage_for_events(self.events)
        self.assertEqual(result["output"], 150)
        self.assertEqual(result["cache_read"], 1000)

    def test_one_hour_cache_writes_bill_above_the_five_minute_rate(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [_turn(cache_write=1000, hour=1000)])
        hour_only = usage_for_events(self.events)["cost"]
        _jsonl(self.transcript, [_turn(cache_write=1000)])
        five_minute = usage_for_events(self.events)["cost"]
        self.assertAlmostEqual(hour_only, 1000 * 1e-05)
        self.assertAlmostEqual(five_minute, 1000 * 6.25e-06)

    def test_rate_is_dollars_per_hour_over_the_trailing_window(self):
        self.point_at_transcript()
        now = datetime.now(timezone.utc)
        _jsonl(
            self.transcript,
            [
                # Outside the window: spent, so it counts in COST but not in $/h.
                _turn(output=1000, when=_stamp(now - timedelta(seconds=usage.RATE_WINDOW + 60))),
                _turn(output=1000, when=_stamp(now - timedelta(seconds=30))),
            ],
        )
        result = usage_for_events(self.events)
        self.assertAlmostEqual(result["cost"], 2 * 1000 * 2.5e-05)
        self.assertAlmostEqual(result["rate"], 1000 * 2.5e-05 * 3600 / usage.RATE_WINDOW)

    def test_untimestamped_turns_give_no_rate(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [_turn(output=10)])
        self.assertIsNone(usage_for_events(self.events)["rate"])

    def test_unknown_pricing_yields_no_cost_rather_than_zero(self):
        self.point_at_transcript()
        _jsonl(
            self.transcript,
            [_turn(model="some-other-model", output=5, when="2026-09-20T12:00:00Z")],
        )
        result = usage_for_events(self.events)
        self.assertIsNone(result["cost"])
        self.assertIsNone(result["rate"])
        self.assertEqual(result["output"], 5)

    def test_model_id_is_passed_through_verbatim(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [_turn(model="claude-haiku-4-5-20251001", output=5)])
        self.assertEqual(usage_for_events(self.events)["model"], "claude-haiku-4-5-20251001")

    def test_limit_comes_from_the_table_for_a_known_model(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [_turn(model="claude-opus-5", input=83_000)])
        self.assertEqual(usage_for_events(self.events)["limit"], 1_000_000)

    def test_limit_is_none_for_a_model_the_table_does_not_cover(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [_turn(model="some-other-model", output=5)])
        self.assertIsNone(usage_for_events(self.events)["limit"])

    def test_synthetic_zero_turn_does_not_become_the_context(self):
        self.point_at_transcript()
        _jsonl(
            self.transcript,
            [_turn(input=5, cache_read=100), _turn(model="<synthetic>", call="msg_zero")],
        )
        result = usage_for_events(self.events)
        self.assertEqual(result["context"], 105)
        self.assertEqual(result["model"], "claude-opus-5")

    def test_no_transcript_path_returns_none(self):
        _jsonl(self.events, [{"hook_event_name": "Stop", "last_assistant_message": ""}])
        self.assertIsNone(usage_for_events(self.events))

    def test_missing_files_return_none(self):
        self.assertIsNone(usage_for_events(self.root / "absent.jsonl"))
        self.point_at_transcript()
        self.assertIsNone(usage_for_events(self.events))

    def test_partial_and_malformed_lines_are_skipped(self):
        self.point_at_transcript()
        self.transcript.write_text(
            json.dumps(_turn(input=4, output=6))
            + "\nnot json\n"
            + json.dumps(["list", "not", "object"])
            + "\n"
            + json.dumps({"type": "assistant", "message": {"usage": "bad"}})
            + "\n"
            + json.dumps({"type": "assistant", "message": None})
            + "\n"
            + json.dumps(_turn(input=1))[:20],
            encoding="utf-8",
        )
        self.assertEqual(usage_for_events(self.events)["input"], 4)

    def test_non_integer_counts_are_ignored(self):
        self.point_at_transcript()
        _jsonl(
            self.transcript,
            [
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {"input_tokens": None, "output_tokens": 3},
                    },
                }
            ],
        )
        result = usage_for_events(self.events)
        self.assertEqual(result["input"], 0)
        self.assertEqual(result["output"], 3)

    def test_transcript_without_assistant_turns_returns_none(self):
        self.point_at_transcript()
        _jsonl(self.transcript, [{"type": "user", "message": {"role": "user"}}])
        self.assertIsNone(usage_for_events(self.events))


class ModelForEventsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.events = self.root / "jack.jsonl"

    def test_model_from_session_start_before_any_transcript_exists(self):
        _jsonl(
            self.events,
            [{"hook_event_name": "SessionStart", "source": "startup", "model": "claude-opus-5"}],
        )
        self.assertEqual(model_for_events(self.events), "claude-opus-5")

    def test_newest_model_wins(self):
        _jsonl(
            self.events,
            [
                {"hook_event_name": "SessionStart", "model": "claude-opus-5"},
                {"hook_event_name": "SessionStart", "model": "claude-sonnet-5"},
            ],
        )
        self.assertEqual(model_for_events(self.events), "claude-sonnet-5")

    def test_missing_file_returns_none(self):
        self.assertIsNone(model_for_events(self.root / "absent.jsonl"))

    def test_no_model_field_returns_none(self):
        _jsonl(self.events, [{"hook_event_name": "Stop", "last_assistant_message": ""}])
        self.assertIsNone(model_for_events(self.events))


class ContextLimitTests(LimitTestCase):
    def test_every_launchable_claude_model_has_a_window(self):
        # The price extract is scoped to models.MODELS; a new entry there needs one in
        # LiteLLM, and PRICES above is the check that it is there today.
        for model in model_ids("claude"):
            with self.subTest(model=model):
                self.assertIsNotNone(context_limit(model))

    def test_every_launchable_claude_model_has_a_bundled_window(self):
        # The fallback is what keeps CTX rendering on a cold cache, so it has to stay in
        # step with models.MODELS even though LiteLLM normally supplies the number.
        for model in model_ids("claude"):
            with self.subTest(model=model):
                self.assertIn(model, usage.CONTEXT_WINDOWS)

    def test_documented_windows(self):
        # LiteLLM max_input_tokens, which matches platform.claude.com "Models overview",
        # Context window row, read 2026-09-20.
        self.assertEqual(context_limit("claude-opus-5"), 1_000_000)
        self.assertEqual(context_limit("claude-sonnet-5"), 1_000_000)
        self.assertEqual(context_limit("claude-fable-5-1"), 1_000_000)
        self.assertEqual(context_limit("claude-haiku-4-5"), 200_000)

    def test_a_live_opus_5_reading_renders_the_percentage_claude_reports(self):
        # `claude /context` in an opus-5 session prints "229.7k/1m tokens (23%)".
        self.assertEqual(round(229_700 / context_limit("claude-opus-5") * 100), 23)

    def test_dated_id_resolves_through_its_alias(self):
        self.assertEqual(context_limit("claude-haiku-4-5-20251001"), 200_000)

    def test_unknown_model_gets_no_limit(self):
        # gpt-5.6-terra is priced and has max_input_tokens 922000, but that is the API
        # window; Codex reads the smaller window its own rollout log states.
        for model in (None, "", "gpt-5.6-terra", "gpt-6-astra", "claude-opus-4-5"):
            with self.subTest(model=model):
                self.assertIsNone(context_limit(model))

    def test_a_cold_price_cache_still_resolves_the_window(self):
        # Live regression: CTX went "-" on every row with no cached prices. A window is
        # a stable fact and must not depend on the network the way COST does.
        os.environ[usage.PRICES_ENV] = str(self.root / "absent.json")
        with patch.object(usage.urllib.request, "urlopen", side_effect=AssertionError("fetched")):
            self.assertEqual(context_limit("claude-opus-5"), 1_000_000)
            self.assertEqual(context_limit("claude-haiku-4-5-20251001"), 200_000)
            self.assertIsNone(context_limit("gpt-5.6-terra"))

    def test_litellm_wins_over_the_bundled_window_when_the_cache_is_warm(self):
        self.prices.write_text(
            json.dumps({"claude-opus-5": {"max_input_tokens": 2_000_000}}), encoding="utf-8"
        )
        self.assertEqual(context_limit("claude-opus-5"), 2_000_000)

    def test_env_override_wins_over_the_table(self):
        os.environ[usage.CONTEXT_LIMIT_ENV] = "500000"
        self.assertEqual(context_limit("claude-opus-5"), 500_000)
        self.assertEqual(context_limit("some-other-model"), 500_000)

    def test_unusable_env_override_falls_back_to_the_table(self):
        for raw in ("", "   ", "lots", "-1", "0", "1e6", "225k"):
            with self.subTest(raw=raw):
                os.environ[usage.CONTEXT_LIMIT_ENV] = raw
                self.assertEqual(context_limit("claude-opus-5"), 1_000_000)


def _codex_counts(total, cached=0, output=0):
    return {
        "input_tokens": total,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "total_tokens": total + output,
    }


def _token_count(total, cached=0, output=0, last=None, window=258_400, when=None):
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": _codex_counts(total, cached, output),
                # Codex's per-turn figures sum exactly to the totals.
                "last_token_usage": last or _codex_counts(total, cached, output),
                "model_context_window": window,
            },
        },
    }
    if when is not None:
        record["timestamp"] = when
    return record


def _turn_context(model):
    return {"type": "turn_context", "payload": {"turn_id": "t", "model": model}}


class CodexUsageTests(LimitTestCase):
    """Codex crew have no transcript; usage comes from the rollout log its notify names."""

    THREAD = "01a0bdb4-132f-7250-b328-05325ae6be1f"
    TITLE_THREAD = "01a0bdb4-22b9-7430-80e9-efa3d3bddfc3"

    def setUp(self):
        super().setUp()
        self.events = self.root / "jack.jsonl"
        self.sessions = self.root / "codex-sessions"
        self.day = self.sessions / "2026" / "09" / "20"
        self.day.mkdir(parents=True)
        self.enterContext(patch.object(usage, "CODEX_SESSIONS_ROOT", self.sessions))

    def rollout(self, thread, records):
        path = self.day / f"rollout-2026-09-20T12-54-50-{thread}.jsonl"
        _jsonl(path, records)
        return path

    def notify(self, *threads):
        _jsonl(self.events, [{"type": "agent-turn-complete", "thread-id": t} for t in threads])

    def test_totals_context_window_and_live_model(self):
        self.notify(self.THREAD)
        self.rollout(
            self.THREAD,
            [
                _turn_context("gpt-5.6-luna"),
                _token_count(20_000, cached=12_000, output=500),
                _turn_context("gpt-5.6-terra"),
                _token_count(
                    101_130,
                    cached=62_464,
                    output=881,
                    last=_codex_counts(27_000, 26_500, 594),
                ),
            ],
        )
        result = usage_for_events(self.events)
        self.assertEqual(
            {name: value for name, value in result.items() if name != "cost"},
            {
                "input": 38_666,
                "output": 881,
                "cache_read": 62_464,
                "cache_write": 0,
                "tokens": 102_011,
                # The last turn's input side only; its 594 output tokens are excluded.
                "context": 27_000,
                "limit": 258_400,
                "model": "gpt-5.6-terra",
                "rate": None,
            },
        )
        # Each turn is priced at the model that was live for it, luna then terra.
        self.assertAlmostEqual(result["cost"], 0.002440 + 0.013428)

    def test_title_generation_thread_has_no_rollout_and_is_skipped(self):
        self.notify(self.THREAD, self.TITLE_THREAD)
        self.rollout(self.THREAD, [_turn_context("gpt-5.6-terra"), _token_count(9, output=1)])
        self.assertEqual(usage_for_events(self.events)["model"], "gpt-5.6-terra")

    def test_ambiguous_thread_id_is_never_guessed(self):
        self.notify(self.THREAD)
        self.rollout(self.THREAD, [_token_count(9, output=1)])
        other = self.sessions / "2026" / "09" / "19"
        other.mkdir(parents=True)
        _jsonl(other / f"rollout-2026-09-19T01-00-00-{self.THREAD}.jsonl", [_token_count(7)])
        self.assertIsNone(usage_for_events(self.events))

    def test_no_notification_yet_returns_none(self):
        _jsonl(self.events, [{"type": "agent-turn-complete", "input-messages": ["build"]}])
        self.assertIsNone(usage_for_events(self.events))

    def test_rollout_without_token_counts_returns_none(self):
        self.notify(self.THREAD)
        self.rollout(self.THREAD, [_turn_context("gpt-5.6-terra")])
        self.assertIsNone(usage_for_events(self.events))

    def test_thread_id_is_not_used_as_a_glob_or_a_path(self):
        _jsonl(self.events, [{"type": "agent-turn-complete", "thread-id": "../*/*/*/rollout-*"}])
        self.assertIsNone(usage_for_events(self.events))

    def test_env_override_wins_over_the_reported_window(self):
        os.environ[usage.CONTEXT_LIMIT_ENV] = "400000"
        self.notify(self.THREAD)
        self.rollout(self.THREAD, [_token_count(9, output=1)])
        self.assertEqual(usage_for_events(self.events)["limit"], 400_000)

    def test_malformed_records_are_skipped(self):
        self.notify(self.THREAD)
        self.rollout(
            self.THREAD,
            [
                {"type": "turn_context", "payload": "not a dict"},
                {"type": "event_msg", "payload": {"type": "token_count", "info": "not a dict"}},
                _token_count(9, output=1),
            ],
        )
        self.assertEqual(usage_for_events(self.events)["context"], 9)


class PriceSourceTests(unittest.TestCase):
    """The price extract: cached under the state root, refreshed off the refresh loop."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_STATE_ROOT": str(self.root)}))
        os.environ.pop(usage.PRICES_ENV, None)
        self.enterContext(patch.object(usage, "_refreshed", False))
        # A temp state root is exactly what memory.state_root() warns about.
        self.enterContext(patch.object(usage.memory, "_warned_temp_state_root", True))

    def test_refresh_keeps_only_the_models_we_launch(self):
        published = {
            "claude-opus-5": {**PRICES["claude-opus-5"], "litellm_provider": "anthropic"},
            "some-other-vendor/model": {"input_cost_per_token": 1.0},
        }
        with patch.object(usage.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                published
            ).encode()
            usage._refresh_prices(self.root / "prices.json")
        cached = json.loads((self.root / "prices.json").read_text(encoding="utf-8"))
        self.assertEqual(list(cached), ["claude-opus-5"])
        self.assertEqual(cached["claude-opus-5"], PRICES["claude-opus-5"])

    def test_a_failed_refresh_leaves_no_cache_and_raises_nothing(self):
        with patch.object(usage.urllib.request, "urlopen", side_effect=OSError("offline")):
            usage._refresh_prices(self.root / "prices.json")
        self.assertFalse((self.root / "prices.json").exists())

    def test_offline_with_a_cache_uses_it_and_never_fetches_inline(self):
        cache = usage._prices_cache()
        cache.write_text(json.dumps(PRICES), encoding="utf-8")
        with patch.object(usage, "_start_refresh") as refresh:
            self.assertEqual(usage._prices()["claude-opus-5"]["max_input_tokens"], 1_000_000)
        refresh.assert_not_called()

    def test_a_stale_cache_is_still_used_while_the_refresh_runs_in_a_thread(self):
        cache = usage._prices_cache()
        cache.write_text(json.dumps(PRICES), encoding="utf-8")
        os.utime(cache, (0, 0))
        with patch.object(usage.threading, "Thread") as thread:
            self.assertIn("claude-opus-5", usage._prices())
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    def test_no_cache_and_no_network_means_no_price_at_all(self):
        # Deliberate: prices get no bundled fallback. A stale bundled price renders a
        # confident wrong number, and "$?" is the honest reading.
        with patch.object(usage, "_start_refresh"):
            self.assertEqual(usage._prices(), {})

    def test_env_override_wins_over_the_cache(self):
        usage._prices_cache().write_text(json.dumps(PRICES), encoding="utf-8")
        override = self.root / "contracted.json"
        override.write_text(
            json.dumps({"claude-opus-5": {"output_cost_per_token": 1e-09}}), "utf-8"
        )
        os.environ[usage.PRICES_ENV] = str(override)
        self.assertEqual(usage._prices()["claude-opus-5"], {"output_cost_per_token": 1e-09})


if __name__ == "__main__":
    unittest.main()
