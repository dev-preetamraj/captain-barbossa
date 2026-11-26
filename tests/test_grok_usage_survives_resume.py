"""Grok usage stays cumulative when its ACP session counters restart after resume."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from captain_barbossa import grok_acp, store, usage


class GrokUsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        events = self.root / "events" / "jack-current.jsonl"
        events.parent.mkdir()
        events.write_text("")
        manifest = self.root / "native-history" / "jack-current.json"
        manifest.parent.mkdir()
        store.write_json(
            manifest,
            {
                "crew_id": "jack",
                "incarnation_id": "current",
                "provider": "grok",
                "project": str(self.root),
                "execution_root": str(self.root),
                "native_root": str(self.root / "grok"),
                "native_alias": str(self.root / "grok"),
                "events": str(events),
                "state": "pending",
                "reason": "writer-running",
                "background": False,
                "ambiguous": False,
                "observed": True,
                "session_ids": [],
                "transcripts": [],
                "deleted_ids": [],
                "anchors": [],
            },
        )
        self.link = SimpleNamespace(events=events, model="grok-4.6")

    def record(self, input_tokens, output_tokens, cost):
        grok_acp.record_usage(
            self.link,
            {
                "usage": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "costUsd": cost,
                    "model": self.link.model,
                }
            },
        )

    def test_totals_survive_a_counter_reset(self):
        self.record(10, 2, 0.10)
        self.record(15, 4, 0.20)
        self.record(15, 4, 0.20)

        self.link.model = "grok-4.7"
        self.record(3, 1, 0.05)
        result = usage.usage_for_events(self.link.events)

        self.assertEqual(
            result,
            {
                "input": 18,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "tokens": 23,
                "context": 3,
                "limit": None,
                "model": "grok-4.7",
                "cost": 0.25,
                "rate": None,
            },
        )

    def test_absent_or_malformed_ledger_is_unknown(self):
        self.assertIsNone(usage.usage_for_events(self.link.events))
        self.link.events.with_suffix(".grok-usage.json").write_text('{"version": 1}')
        self.assertIsNone(usage.usage_for_events(self.link.events))


if __name__ == "__main__":
    unittest.main()
