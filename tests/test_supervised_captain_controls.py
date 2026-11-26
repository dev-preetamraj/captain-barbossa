"""Each supervised adapter changes model and effort through its own native call, reports what is
current, and lists the running agent's commands and skills."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import home_isolation  # noqa: F401

from captain_barbossa import claude_agent, codex_app_server, pi_rpc
from captain_barbossa.runtime import CaptainError

CODEX_MODELS = {
    "data": [
        {
            "model": "gpt-a",
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
        },
        {"model": "gpt-b", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]},
    ],
    "nextCursor": None,
}


class FakePeer:
    """Answers each request from `answers` (a value or a callable of params) and records it."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def request(self, method, params, timeout):
        self.sent.append((method, params))
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    def notify(self, method, timeout):
        self.sent.append((method, None))

    def start(self):
        pass

    def methods(self):
        return [method for method, _ in self.sent]


def link(peer=None, **fields):
    values = dict(
        peer=peer,
        native={},
        model=None,
        turn=None,
        guard=threading.Lock(),
        captain=True,
        root="/work",
        crew_id="captain",
        stdin=3,
        stdout=4,
        thread=None,
        writable=(),
        events=None,
    )
    values.update(fields)
    return SimpleNamespace(**values)


class ClaudeControlTests(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer({"connect": {"session_id": "sid", "commands": self.listed()}})

    def listed(self):
        return [
            {"name": "compact", "description": "Clear history but keep a summary"},
            {"name": "review-pr", "description": "Review a pull request", "argumentHint": ""},
        ]

    def opened(self, **fields):
        agent = link(**fields)
        with patch.object(claude_agent.jsonrpc, "Peer", return_value=self.peer):
            claude_agent.open(agent)
        return agent

    def test_a_start_effort_goes_to_the_host_connect(self):
        agent = self.opened(effort="max")
        self.assertEqual(self.peer.sent, [("connect", {"ask": True, "effort": "max"})])
        self.assertEqual(claude_agent.current(agent), {"model": None, "effort": "max"})

    def test_an_unknown_start_effort_is_refused_before_claude_starts(self):
        with self.assertRaisesRegex(CaptainError, r"^\[effort-unsupported\]"):
            self.opened(effort="turbo")
        self.assertEqual(self.peer.sent, [])

    def test_a_model_change_with_effort_uses_claudes_set_model_call(self):
        agent = self.opened()
        changed = claude_agent.set_model(agent, "claude-opus-5-5", "high")
        self.assertEqual(
            self.peer.sent[-1], ("set_model", {"model": "claude-opus-5-5", "effort": "high"})
        )
        self.assertEqual(changed, {"model": "claude-opus-5-5", "effort": "high", "applies": "now"})

    def test_a_crew_model_change_sends_only_the_model(self):
        agent = self.opened(captain=False)
        claude_agent.set_model(agent, "claude-sonnet-5-5")
        self.assertEqual(self.peer.sent[-1], ("set_model", {"model": "claude-sonnet-5-5"}))

    def test_max_effort_is_refused_mid_session(self):
        agent = self.opened()
        with self.assertRaisesRegex(CaptainError, r"^\[effort-unsupported\]"):
            claude_agent.set_model(agent, None, "max")
        self.assertEqual(self.peer.methods(), ["connect"])

    def test_the_init_message_names_the_model_and_the_skills(self):
        agent = self.opened()
        init = {"model": "claude-opus-5-5", "skills": ["review-pr"], "slash_commands": []}
        data = {"type": "SystemMessage", "subtype": "init", "data": init}
        claude_agent.handle(
            agent, {"method": "message", "params": {"kind": "SystemMessage", "data": data}}
        )
        self.assertEqual(claude_agent.current(agent)["model"], "claude-opus-5-5")
        self.assertEqual(
            claude_agent.commands(agent),
            {
                "commands": [
                    {"name": "compact", "description": "Clear history but keep a summary"}
                ],
                "skills": [{"name": "review-pr", "description": "Review a pull request"}],
            },
        )


class CodexControlTests(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer(
            {
                "initialize": {},
                "thread/start": {
                    "thread": {"id": "thread-1"},
                    "model": "gpt-a",
                    "reasoningEffort": "low",
                },
                "model/list": CODEX_MODELS,
                "turn/start": {"turn": {"id": "turn-1"}},
                "skills/list": {
                    "data": [
                        {
                            "cwd": "/work",
                            "errors": [],
                            "skills": [
                                {"name": "ship", "description": "Ship it", "enabled": True},
                                {"name": "off", "description": "Disabled", "enabled": False},
                            ],
                        }
                    ]
                },
            }
        )

    def opened(self, **fields):
        agent = link(**fields)
        with patch.object(codex_app_server.jsonrpc, "Peer", return_value=self.peer):
            codex_app_server.open(agent)
        return agent

    def test_the_thread_reports_its_model_and_effort(self):
        agent = self.opened()
        self.assertEqual(codex_app_server.current(agent), {"model": "gpt-a", "effort": "low"})

    def test_a_start_effort_rides_every_turn(self):
        agent = self.opened(effort="high")
        codex_app_server.ring(agent, "m1", "hello")
        started = dict(self.peer.sent)["turn/start"]
        self.assertEqual(started["effort"], "high")
        self.assertEqual(codex_app_server.current(agent)["effort"], "high")

    def test_an_effort_the_model_does_not_list_is_refused(self):
        with self.assertRaisesRegex(CaptainError, r"^\[effort-unsupported\].*low, high"):
            self.opened(effort="xhigh")
        agent = self.opened()
        with self.assertRaisesRegex(CaptainError, r"^\[effort-unsupported\].*medium"):
            codex_app_server.set_model(agent, "gpt-b", "high")
        self.assertEqual(agent.model, None)

    def test_a_model_change_drops_a_kept_effort_the_new_model_lacks(self):
        agent = self.opened(effort="high")
        changed = codex_app_server.set_model(agent, "gpt-b")
        self.assertEqual(changed, {"model": "gpt-b", "effort": None, "applies": "now"})
        codex_app_server.ring(agent, "m1", "hello")
        started = dict(self.peer.sent)["turn/start"]
        self.assertEqual((started["model"], "effort" in started), ("gpt-b", False))

    def test_commands_are_the_enabled_skills_and_the_app_served_builtins(self):
        agent = self.opened()
        listed = codex_app_server.commands(agent)
        self.assertEqual(self.peer.sent[-1], ("skills/list", {"cwds": ["/work"]}))
        self.assertEqual(listed["skills"], [{"name": "ship", "description": "Ship it"}])
        self.assertEqual(
            [row["name"] for row in listed["commands"]], ["compact", "review", "model"]
        )


class PiControlTests(unittest.TestCase):
    def setUp(self):
        self.state = {"provider": "anthropic", "id": "claude-a", "level": "medium"}
        self.levels = {"anthropic/claude-a": ["off", "low", "medium", "high"]}
        self.levels["openai/gpt-b"] = ["off", "low"]

        def get_state(params):
            model = {"provider": self.state["provider"], "id": self.state["id"]}
            return {
                "isStreaming": False,
                "sessionId": "pi-session",
                "model": model,
                "thinkingLevel": self.state["level"],
            }

        def set_model(params):
            self.state.update(provider=params["provider"], id=params["modelId"])

        def levels(params):
            return {"levels": self.levels[f"{self.state['provider']}/{self.state['id']}"]}

        def set_level(params):
            self.state["level"] = params["level"]

        self.peer = FakePeer(
            {
                "get_state": get_state,
                "set_model": set_model,
                "get_available_thinking_levels": levels,
                "set_thinking_level": set_level,
                "get_commands": {
                    "commands": [
                        {"name": "fix-tests", "description": "Fix tests", "source": "prompt"},
                        {"name": "skill:ship", "description": "Ship it", "source": "skill"},
                    ]
                },
            }
        )

    def opened(self, **fields):
        agent = link(**fields)
        with patch.object(pi_rpc, "Peer", return_value=self.peer):
            pi_rpc.open(agent)
        return agent

    def test_a_start_effort_sets_pis_thinking_level(self):
        agent = self.opened(effort="high")
        self.assertEqual(self.peer.sent[-1], ("set_thinking_level", {"level": "high"}))
        self.assertEqual(pi_rpc.current(agent), {"model": "anthropic/claude-a", "effort": "high"})

    def test_a_model_and_effort_change_uses_pis_own_calls(self):
        agent = self.opened()
        changed = pi_rpc.set_model(agent, "openai/gpt-b", "low")
        self.assertEqual(changed, {"model": "openai/gpt-b", "effort": "low", "applies": "now"})
        self.assertIn(("set_model", {"provider": "openai", "modelId": "gpt-b"}), self.peer.sent)

    def test_a_refused_level_restores_the_previous_model(self):
        agent = self.opened()
        with self.assertRaisesRegex(CaptainError, r"^\[effort-unsupported\].*off, low"):
            pi_rpc.set_model(agent, "openai/gpt-b", "high")
        self.assertEqual((self.state["provider"], self.state["id"]), ("anthropic", "claude-a"))
        self.assertEqual(agent.model, None)

    def test_commands_split_skills_from_the_rest(self):
        agent = self.opened()
        self.assertEqual(
            pi_rpc.commands(agent),
            {
                "commands": [{"name": "fix-tests", "description": "Fix tests"}],
                "skills": [{"name": "skill:ship", "description": "Ship it"}],
            },
        )


if __name__ == "__main__":
    unittest.main()
