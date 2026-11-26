"""Supervised agents' live usage: each native protocol's reading becomes one frame, kept beside
the events file, published to the app and filling only what the native files leave unknown."""

import asyncio
import contextlib
import queue
import threading
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    claude_agent,
    claude_host,
    codex_app_server,
    dashboard,
    pi_rpc,
    sessions,
    store,
    supervisor,
    usage,
)

PI_RECORD = {
    "id": "jack",
    "name": "Jack",
    "agent": "c-jack",
    "provider": "pi",
    "model": "anthropic/claude-sonnet-5-5",
    "status": "working",
    "incarnation_id": "a" * 32,
}
PI_STATS = {
    "tokens": {"input": 500, "output": 100, "cacheRead": 400, "cacheWrite": 0, "total": 1000},
    "cost": 0.25,
    "contextUsage": {"tokens": 60000, "contextWindow": 200000, "percent": 30},
}
CODEX_USAGE = {
    "total": {
        "inputTokens": 1200,
        "cachedInputTokens": 200,
        "outputTokens": 50,
        "reasoningOutputTokens": 10,
        "totalTokens": 1250,
    },
    "last": {
        "inputTokens": 900,
        "cachedInputTokens": 100,
        "outputTokens": 20,
        "reasoningOutputTokens": 0,
        "totalTokens": 920,
    },
    "modelContextWindow": 258400,
}


class LiveFrameTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.events = self.root / "events" / "jack-abc.jsonl"
        self.events.parent.mkdir()
        self.events.write_text("")

    def test_codex_token_usage_becomes_a_frame_with_no_invented_cost(self):
        frame = usage.codex_live(CODEX_USAGE, "gpt-6.2-sol")
        self.assertEqual(
            {key: frame[key] for key in ("input", "cache_read", "output", "tokens")},
            {"input": 1000, "cache_read": 200, "output": 50, "tokens": 1250},
        )
        self.assertEqual((frame["context"], frame["limit"]), (900, 258400))
        self.assertIsNone(frame["cost"])
        self.assertIsNone(frame["rate"])
        self.assertIsNone(usage.codex_live({"last": {}}, "gpt-6.2-sol"))

    def test_pi_stats_without_a_context_window_leave_ctx_unknown(self):
        frame = usage.pi_live(PI_STATS, "anthropic/claude-sonnet-5-5")
        self.assertEqual((frame["tokens"], frame["cost"]), (1000, 0.25))
        self.assertEqual((frame["context"], frame["limit"]), (60000, 200000))
        bare = usage.pi_live({**PI_STATS, "contextUsage": None, "cost": None}, None)
        self.assertEqual((bare["context"], bare["limit"], bare["cost"]), (None, None, None))
        self.assertIsNone(usage.pi_live({"tokens": {"input": -1}}, None))

    def test_claude_results_add_up_and_the_session_cost_is_taken_as_reported(self):
        totals = {}
        first = {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.1}
        usage.claude_live(totals, first, "claude-opus-5-5")
        second = {
            "usage": {"input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 100},
            "total_cost_usd": 0.3,
            "model_usage": {"claude-opus-5-5": {"contextWindow": 1000000}},
        }
        frame = usage.claude_live(totals, second, "claude-opus-5-5")
        self.assertEqual((frame["input"], frame["output"], frame["tokens"]), (30, 10, 140))
        self.assertEqual((frame["cost"], frame["limit"], frame["context"]), (0.3, 1000000, None))

    def test_live_fills_only_what_the_native_reading_leaves_unknown(self):
        native = {**dict.fromkeys(("input", "output", "cache_read", "cache_write"), 1)}
        native.update(tokens=4, context=None, limit=None, model="m", cost=0.5, rate=None)
        live = usage.pi_live(PI_STATS, "other")
        usage.record_live(self.events, live)
        with patch.object(usage, "usage_for_events", return_value=native):
            merged = usage.with_live(self.events)
        self.assertEqual((merged["tokens"], merged["cost"], merged["model"]), (4, 0.5, "m"))
        self.assertEqual((merged["context"], merged["limit"]), (60000, 200000))
        with patch.object(usage, "usage_for_events", return_value=None):
            self.assertEqual(usage.with_live(self.events)["tokens"], 1000)

    def test_a_malformed_ledger_reads_as_unknown_never_zero(self):
        store.write_json(
            self.events.with_suffix(usage.LIVE_SUFFIX),
            {"version": 1, "usage": {"tokens": -5, "cost": "free", "context": True}},
        )
        frame = usage.live_usage(self.events)
        self.assertEqual((frame["tokens"], frame["cost"], frame["context"]), (None, None, None))
        store.write_json(self.events.with_suffix(usage.LIVE_SUFFIX), {"version": 2})
        self.assertIsNone(usage.live_usage(self.events))


class AdapterReadingTests(SessionCase):
    def link(self, **fields):
        frames = []
        link = SimpleNamespace(
            crew_id="jack",
            model="gpt-6.2-sol",
            native={},
            guard=threading.Lock(),
            usage=frames.append,
            **fields,
        )
        return link, frames

    def test_codex_token_usage_notifications_reach_the_supervisor_in_order(self):
        link, frames = self.link()
        for total in (100, 200):
            params = {"tokenUsage": {**CODEX_USAGE, "total": {"inputTokens": total}}}
            codex_app_server.handle(link, {"method": "thread/tokenUsage/updated", "params": params})
        self.assertEqual([frame["input"] for frame in frames], [100, 200])

    def test_claude_results_accumulate_per_crew(self):
        link, frames = self.link()
        result = {"usage": {"input_tokens": 7}, "total_cost_usd": 0.2, "model": "claude-x"}
        for _ in range(2):
            claude_agent.handle(link, {"method": "result", "params": result})
        self.assertEqual([frame["input"] for frame in frames], [7, 14])
        self.assertEqual(frames[-1]["model"], "claude-x")

    def test_pi_session_stats_are_read_after_each_settled_turn(self):
        asked = []

        class Peer:
            def request(self, command, params, timeout):
                asked.append(command)
                return PI_STATS

        link, frames = self.link(peer=Peer())
        pi_rpc.record_stats(link)
        self.assertEqual(asked, ["get_session_stats"])
        self.assertEqual(frames[0]["context"], 60000)

    def test_a_failed_stats_read_publishes_nothing(self):
        class Peer:
            def request(self, command, params, timeout):
                raise supervisor.CaptainError("[pi-rpc-timeout] no answer")

        link, frames = self.link(peer=Peer())
        pi_rpc.record_stats(link)
        self.assertEqual(frames, [])


@dataclass
class ResultMessage:
    subtype: str
    is_error: bool = False
    usage: dict = field(default_factory=lambda: {"input_tokens": 3})
    total_cost_usd: float = 0.01


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-opus-5-5"


class StreamEvent:
    def __init__(self, event):
        self.event = event


class ClaudeHostOutputTests(SessionCase):
    def test_every_sdk_message_and_each_result_reach_the_output_stream(self):
        host = claude_host.Host(["/opt/bin/claude"], 1)
        sent = []
        host.send = sent.append
        host.sdk = SimpleNamespace(ResultMessage=ResultMessage, AssistantMessage=AssistantMessage)
        host.observe(StreamEvent({"type": "content_block_delta"}))
        host.observe(AssistantMessage([TextBlock("Hello")]))
        host.observe(ResultMessage("success"))
        methods = [message["method"] for message in sent]
        self.assertEqual(methods, ["message", "turn", "message", "result", "turn"])
        self.assertEqual(sent[0]["params"]["kind"], "StreamEvent")
        content = sent[2]["params"]["data"]["content"]
        self.assertEqual(content, [{"type": "TextBlock", "text": "Hello"}])
        self.assertEqual(sent[3]["params"]["model"], "claude-opus-5-5")
        self.assertEqual(sent[3]["params"]["usage"], {"input_tokens": 3})

    def test_an_oversized_message_is_dropped_without_ending_the_turn(self):
        host = claude_host.Host(["/opt/bin/claude"], 1)

        def send(message):
            if message["method"] == "message":
                raise claude_host.CaptainError("[jsonrpc-line] too long")

        host.send = send
        host.sdk = SimpleNamespace(ResultMessage=ResultMessage, AssistantMessage=AssistantMessage)
        host.observe(AssistantMessage(["x"]))
        self.assertTrue(host.working)

    def test_only_a_supervised_host_streams_partial_messages(self):
        sdk = SimpleNamespace(ClaudeAgentOptions=lambda **given: SimpleNamespace(given=given))
        built, _ = claude_host.options(sdk, ["/c"], "/work", partial=True)
        self.assertTrue(built.given["include_partial_messages"])
        self.assertNotIn(
            "include_partial_messages", claude_host.options(sdk, ["/c"], "/w")[0].given
        )


class PublishedUsageTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["jack"] = {**PI_RECORD, "execution": "supervised"}
        self.supervisor = supervisor.Supervisor(self.current, "token", threading.Event())
        self.feed = supervisor.join(self.supervisor)
        self.events = self.current.events(f"jack-{PI_RECORD['incarnation_id']}")
        store.private_dir(self.events.parent)
        self.events.write_text("")

    def test_a_reading_is_kept_published_and_served_by_usage(self):
        link = SimpleNamespace(crew_id="jack", events=self.events)
        frame = usage.pi_live(PI_STATS, PI_RECORD["model"])
        supervisor.live_usage(self.supervisor, link, frame)
        method, params = self.feed.get_nowait()
        self.assertEqual((method, params["method"]), ("output", supervisor.USAGE_OUTPUT))
        self.assertEqual(params["params"]["context"], 60000)
        rows = supervisor.agent_usage(self.supervisor, {})["agents"]
        self.assertEqual([row["agent"] for row in rows], ["jack"])
        self.assertEqual(rows[0]["usage"]["limit"], 200000)

    def test_an_agent_with_no_reading_is_unknown(self):
        rows = supervisor.agent_usage(self.supervisor, {})["agents"]
        self.assertEqual(rows, [{"agent": "jack", "usage": None}])
        with self.assertRaises(queue.Empty):
            self.feed.get_nowait()

    def test_the_supervised_dashboard_shows_live_ctx(self):
        usage.record_live(self.events, usage.pi_live(PI_STATS, PI_RECORD["model"]))
        frame = dashboard.render(self.current, size=(100, 20))
        self.assertRegex(frame, r"Jack .*30%")


class HerdrDashboardTests(SessionCase):
    def test_a_herdr_dashboard_ignores_any_live_ledger(self):
        current = sessions.session(self.project, self.pane, create=True)
        with sessions.crew_meta(current.directory) as meta:
            meta["crew"]["jack"] = dict(PI_RECORD)
        current = sessions.Session(current.directory, meta)
        events = current.events(f"jack-{PI_RECORD['incarnation_id']}")
        store.private_dir(events.parent)
        events.write_text("")
        herdr = patch.object(dashboard.runtime, "herdr", return_value={"agents": []})
        with herdr, patch("time.strftime", return_value="12:00:00"):
            before = dashboard.render(current, size=(100, 20))
            usage.record_live(events, usage.pi_live(PI_STATS, PI_RECORD["model"]))
            self.assertEqual(dashboard.render(current, size=(100, 20)), before)
        self.assertNotIn("30%", before)


class HostRequestTests(SessionCase):
    def test_a_host_request_resolves_with_the_supervisors_reply(self):
        host = claude_host.Host(["/opt/bin/claude"], 1)

        async def scenario():
            host.send = lambda message: asyncio.get_running_loop().call_soon(
                host.dispatch, {"id": message["id"], "result": {"behavior": "allow"}}
            )
            return await host.request("permission", {}, 5)

        self.assertEqual(asyncio.run(scenario())["result"], {"behavior": "allow"})
        self.assertEqual(host.waiting, {})


if __name__ == "__main__":
    unittest.main()
