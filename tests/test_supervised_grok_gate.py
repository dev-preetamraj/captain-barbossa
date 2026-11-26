"""A supervised Grok crew's turns and gate: a turn may outlast any request deadline, a prompt
lands only once sent, every effect reaches the gate in its own terms, and a call that skips the
gate cancels the turn."""

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, grok_acp, jsonrpc, store
from captain_barbossa.runtime import CaptainError


def link_for(root, peer=None):
    calls = {"relay": [], "ended": [], "notice": []}
    link = SimpleNamespace(
        crew_id="jack",
        thread="session-1",
        root=str(root),
        model="grok-4.7",
        turn=None,
        native={},
        guard=threading.Lock(),
        events=Path(root) / "events" / "jack-a.jsonl",
        peer=peer,
        relay=lambda event: calls["relay"].append(event) or (0, None),
        ended=lambda event: calls["ended"].append(event),
        notice=lambda key, summary: calls["notice"].append((key, summary)),
        usage=None,
        stopping=threading.Event(),
        stdin=None,
        stdout=None,
    )
    link.events.parent.mkdir(parents=True, exist_ok=True)
    return link, calls


class Server:
    """An ACP peer on pipes that answers session/prompt only when told to."""

    def __init__(self):
        self.to_client_read, self.to_client_write = os.pipe()
        self.to_server_read, self.to_server_write = os.pipe()
        self.received = []
        self.reader = jsonrpc.LineReader(self.to_server_read)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            while (message := self.reader.read()) is not None:
                self.received.append(message)
        except (CaptainError, OSError):
            pass

    def reply(self, identifier, result):
        message = {"jsonrpc": "2.0", "id": identifier, "result": result}
        jsonrpc.write(self.to_client_write, message, time.monotonic() + 5)

    def peer(self, handler):
        peer = jsonrpc.Peer(self.to_client_read, self.to_server_write, handler, "grok-test")
        peer.start()
        return peer


class GrokTurnTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(settings / "settings.toml", "[supervisor]\nrequest_timeout=0.2\n")
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_PROJECT": str(self.project)}))

    def until(self, check, what, wait=5):
        deadline = time.monotonic() + wait
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)
        return found

    def test_a_turn_longer_than_the_request_deadline_still_completes(self):
        server = Server()
        link, calls = link_for(self.root)
        link.peer = server.peer(lambda message: grok_acp.handle(link, message))
        self.assertEqual(grok_acp.ring(link, "m1", "Do it."), {"landed": True, "gate": None})
        prompt = self.until(lambda: server.received, "the prompt")[0]
        self.assertEqual(prompt["method"], "session/prompt")
        time.sleep(0.5)  # past request_timeout
        self.assertEqual(link.turn, "turn-1")
        self.assertEqual(calls["notice"], [])
        usage = {"inputTokens": 12, "outputTokens": 3}
        server.reply(prompt["id"], {"stopReason": "end_turn", "usage": usage})
        self.until(lambda: calls["ended"], "the turn to end")
        self.assertIsNone(link.turn)
        self.assertEqual(calls["relay"][0]["type"], "agent-turn-complete")
        ledger = store.read_json(link.events.with_suffix(".grok-usage.json"))
        self.assertEqual(ledger["totals"]["input"], 12)

    def test_a_prompt_that_cannot_be_sent_never_lands(self):
        server = Server()
        link, calls = link_for(self.root)
        link.peer = server.peer(lambda message: None)
        link.peer.close()
        with patch.object(link.peer, "closed", "gone"), self.assertRaises(CaptainError):
            grok_acp.ring(link, "m1", "Do it.")
        self.assertIsNone(link.turn)
        self.assertNotIn("m1", link.native.get("accepted", set()))

    def test_a_closed_connection_ends_the_turn_quietly_while_stopping(self):
        server = Server()
        link, calls = link_for(self.root)
        link.peer = server.peer(lambda message: None)
        grok_acp.ring(link, "m1", "Do it.")
        self.until(lambda: server.received, "the prompt")
        link.stopping.set()
        os.close(server.to_client_write)
        self.until(lambda: link.turn is None, "the turn to clear")
        self.assertEqual(calls["notice"], [])


class StubPeer:
    def __init__(self, replies=None):
        self.replies, self.requests, self.sent = replies or {}, [], []

    def start(self):
        pass

    def request(self, method, params, timeout):
        self.requests.append((method, params))
        return self.replies.get(method, {})

    def _send(self, message, deadline):
        self.sent.append(message)


class GrokGateTests(SessionCase):
    def event(self, kind, raw=None, locations=()):
        call = {"toolCallId": "t1", "kind": kind, "rawInput": raw or {}}
        call["locations"] = [{"path": path} for path in locations]
        link, _ = link_for(self.root)
        return grok_acp._permission_event(link, {"sessionId": "s", "toolCall": call})

    def test_a_delete_reaches_the_gate_as_a_removal(self):
        event = self.event("delete", locations=["/repo/notes.md"])
        self.assertEqual(
            (event["tool_name"], event["tool_input"]), ("Bash", {"command": "rm -- /repo/notes.md"})
        )
        moved = self.event("move", locations=["/repo/a.md", "/repo/b.md"])
        self.assertEqual(moved["tool_input"], {"command": "mv /repo/a.md /repo/b.md"})

    def test_an_edit_takes_its_path_from_locations_and_is_whole_unless_partial(self):
        whole = self.event("edit", {"content": "x"}, ["/repo/notes.md"])
        self.assertEqual(
            (whole["tool_name"], whole["tool_input"]), ("Write", {"file_path": "/repo/notes.md"})
        )
        partial = self.event(
            "edit", {"old_string": "a", "new_string": "b", "file_path": "/repo/n.md"}
        )
        self.assertEqual(partial["tool_name"], "Edit")

    def test_an_effect_with_no_single_path_reaches_the_gate_unplaced(self):
        for kind, places in (
            ("edit", ()),
            ("edit", ("/a", "/b")),
            ("delete", ()),
            ("move", ("/a",)),
        ):
            with self.subTest(kind=kind, places=places):
                event = self.event(kind, locations=places)
                self.assertEqual((event["tool_name"], event["tool_input"]), ("Edit", {}))

    def test_a_call_that_never_asked_the_gate_cancels_the_turn(self):
        peer = StubPeer()
        link, calls = link_for(self.root, peer)
        link.turn = "turn-1"

        def update(**fields):
            params = {"sessionId": "s", "update": fields}
            grok_acp.handle(link, {"method": "session/update", "params": params})

        update(sessionUpdate="tool_call", toolCallId="asked", kind="execute", status="pending")
        request = {"sessionId": "s", "toolCall": {"toolCallId": "asked"}, "options": []}
        with patch.object(grok_acp, "answer"):
            grok_acp.handle(
                link, {"id": 9, "method": "session/request_permission", "params": request}
            )
        update(sessionUpdate="tool_call_update", toolCallId="asked", status="in_progress")
        update(sessionUpdate="tool_call", toolCallId="read", kind="read", status="in_progress")
        update(sessionUpdate="tool_call", toolCallId="sneak", kind="edit", status="pending")
        update(sessionUpdate="tool_call_update", toolCallId="sneak", status="completed")
        deadline = time.monotonic() + 5
        while not calls["notice"] and time.monotonic() < deadline:
            time.sleep(0.02)
        ((key, summary),) = calls["notice"]
        self.assertEqual(key, "grok_ungated")
        self.assertIn("edit call", summary)
        self.assertEqual([message["method"] for message in peer.sent], ["session/cancel"])

    def test_a_session_is_switched_to_an_asking_mode_and_a_missing_version_is_read_from_the_cli(
        self,
    ):
        modes = {"currentModeId": "auto", "availableModes": [{"id": "auto"}, {"id": "default"}]}
        peer = StubPeer(
            {"initialize": {}, "session/new": {"sessionId": "s1", "modes": modes}},
        )
        link, _ = link_for(self.root)
        with (
            patch.object(grok_acp.jsonrpc, "Peer", return_value=peer),
            patch.object(grok_acp, "cli_version", return_value="grok 1.0.62"),
        ):
            grok_acp.open(link)
        methods = [method for method, _ in peer.requests]
        self.assertEqual(
            methods, ["initialize", "session/new", "session/set_mode", "session/set_model"]
        )
        self.assertEqual(peer.requests[2][1], {"sessionId": "s1", "modeId": "default"})
        with (
            patch.object(grok_acp.jsonrpc, "Peer", return_value=StubPeer()),
            patch.object(grok_acp, "cli_version", return_value=None),
            self.assertRaisesRegex(CaptainError, r"\[grok-acp-version\]"),
        ):
            grok_acp.open(link_for(self.root)[0])
