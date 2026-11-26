"""Transcripts page through each provider's own session file, never a copy, and a step returns
one entry's whole block from the same native record."""

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    config,
    jsonrpc,
    sessions,
    store,
    supervision,
    supervisor,
    transcripts,
    usage,
)
from captain_barbossa.runtime import CaptainError

INCARNATION = "b" * 32
THREAD = "01a2176a-48b1-70c0-a026-b307ee836b13"


def lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record) + "\n")


class SupervisedTranscriptTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        self.native = self.root / "native"

    def crew(self, provider, name="will"):
        record = {"id": name, "incarnation_id": INCARNATION, "provider": provider}
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"][name] = record
        self.current = self.current._replace(meta=meta)
        return self.current.events(f"{name}-{INCARNATION}")

    def claude(self):
        events = self.crew("claude")
        transcript = self.native / "projects" / "p" / "s.jsonl"
        lines(events, [{"hook_event_name": "SessionStart", "transcript_path": str(transcript)}])
        lines(
            transcript,
            [
                {"type": "summary", "summary": "skipped"},
                {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "Go."}},
                {
                    "type": "assistant",
                    "timestamp": "t2",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Plan."},
                            {"type": "text", "text": "Reading."},
                            {
                                "type": "tool_use",
                                "id": "tu1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "t3",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu1",
                                "content": [{"type": "text", "text": "x" * 5000}],
                            }
                        ],
                    },
                },
            ],
        )
        return transcript

    def test_a_claude_transcript_pages_entries_and_steps_return_whole_blocks(self):
        self.claude()
        page = transcripts.read(self.current, "will")
        self.assertEqual(page["provider"], "claude")
        self.assertEqual(
            [(e["role"], e["kind"], e["tool"], e["call"]) for e in page["entries"]],
            [
                ("user", "text", None, None),
                ("assistant", "thinking", None, None),
                ("assistant", "text", None, None),
                ("assistant", "tool_call", "Bash", "tu1"),
                ("tool", "tool_result", None, "tu1"),
            ],
        )
        self.assertEqual(page["entries"][3]["text"], '{"command": "ls"}')
        result = page["entries"][4]
        self.assertEqual((len(result["text"]), result["clipped"]), (transcripts.TEXT_CHARS, True))
        whole = transcripts.step(self.current, "will", result["id"])
        self.assertEqual((whole["text"], whole["clipped"], whole["at"]), ("x" * 5000, False, "t3"))
        self.assertFalse(page["more"])

    def test_pages_go_back_with_before_and_forward_with_after_without_splitting_a_record(self):
        transcript = self.claude()
        newest = transcripts.read(self.current, "will", limit=2)
        # The last record holds one entry, so the one before it comes whole: four, not two.
        self.assertEqual(
            [e["kind"] for e in newest["entries"]],
            ["thinking", "text", "tool_call", "tool_result"],
        )
        self.assertTrue(newest["more"])
        oldest = transcripts.read(self.current, "will", before=newest["start"], limit=2)
        self.assertEqual([e["text"] for e in oldest["entries"]], ["Go."])
        self.assertFalse(oldest["more"])

        lines(
            transcript,
            [{"type": "assistant", "message": {"role": "assistant", "content": "Done."}}],
        )
        newer = transcripts.read(self.current, "will", after=newest["end"])
        self.assertEqual([e["text"] for e in newer["entries"]], ["Done."])
        self.assertEqual(transcripts.read(self.current, "will", after=newer["end"])["entries"], [])
        first = transcripts.read(self.current, "will", after="0:0", limit=1)
        self.assertEqual(([e["text"] for e in first["entries"]], first["more"]), (["Go."], True))

    def test_a_codex_rollout_is_found_by_its_thread(self):
        events = self.crew("codex")
        lines(events, [{"type": "agent-turn-complete", "thread-id": THREAD}])
        rollout = self.native / "sessions" / "2026" / "11" / "26" / f"rollout-x-{THREAD}.jsonl"
        lines(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": THREAD}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "Rules."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Mail."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {"type": "reasoning", "summary": [], "encrypted_content": "x"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "c1",
                        "name": "exec",
                        "input": "ls",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "c1",
                        "output": [{"type": "input_text", "text": "a.py"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "c2",
                        "name": "sleep",
                        "arguments": "{}",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": "c2", "output": "slept"},
                },
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "dup"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}],
                    },
                },
            ],
        )
        with patch.object(usage, "CODEX_SESSIONS_ROOT", self.native / "sessions"):
            page = transcripts.read(self.current, "will")
        self.assertEqual(page["files"], [str(rollout)])
        self.assertEqual(
            [(e["role"], e["kind"], e["tool"], e["text"]) for e in page["entries"]],
            [
                ("system", "text", None, "Rules."),
                ("user", "text", None, "Mail."),
                ("assistant", "tool_call", "exec", "ls"),
                ("tool", "tool_result", None, "a.py"),
                ("assistant", "tool_call", "sleep", "{}"),
                ("tool", "tool_result", None, "slept"),
                ("assistant", "text", None, "Done."),
            ],
        )

    def test_a_pi_session_file_comes_from_the_native_history_manifest(self):
        events = self.crew("pi")
        root = self.current.directory / "native-history" / f"pi-{INCARNATION}"
        session_file = root / "2026-11-25_s1.jsonl"
        lines(
            session_file,
            [
                {"type": "session", "id": "s1"},
                {
                    "type": "message",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Go."}]},
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Hmm."},
                            {
                                "type": "toolCall",
                                "id": "k1",
                                "name": "bash",
                                "arguments": {"command": "ls"},
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "k1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "a.py"}],
                    },
                },
            ],
        )
        manifest = {
            **dict.fromkeys(("crew_id", "project", "execution_root", "state", "reason"), "x"),
            "incarnation_id": INCARNATION,
            "provider": "pi",
            "native_root": str(root),
            "native_alias": str(root),
            "events": str(events),
            "background": False,
            "ambiguous": False,
            "observed": True,
            "session_ids": ["s1"],
            "transcripts": [str(session_file)],
            "deleted_ids": [],
            "anchors": [],
        }
        store.write_json(root.parent / f"will-{INCARNATION}.json", manifest)
        page = transcripts.read(self.current, "will")
        self.assertEqual(
            [(e["role"], e["kind"], e["tool"], e["call"]) for e in page["entries"]],
            [
                ("user", "text", None, None),
                ("assistant", "thinking", None, None),
                ("assistant", "tool_call", "bash", "k1"),
                ("tool", "tool_result", "bash", "k1"),
            ],
        )

    def test_bad_agents_cursors_and_entries_are_typed_refusals(self):
        self.claude()
        for call, code in (
            (lambda: transcripts.read(self.current, "nobody"), "transcript-agent"),
            (lambda: transcripts.read(self.current, "will", after="x"), "transcript-range"),
            (lambda: transcripts.read(self.current, "will", limit=0), "transcript-range"),
            (
                lambda: transcripts.read(self.current, "will", after="0:0", before="0:9"),
                "transcript-range",
            ),
            (lambda: transcripts.step(self.current, "will", "0:3:0"), "transcript-entry"),
            (lambda: transcripts.step(self.current, "will", "0:0:9"), "transcript-entry"),
            (lambda: transcripts.step(self.current, "will", "5:0:0"), "transcript-entry"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(CaptainError, rf"\[{code}\]"):
                call()
        self.assertEqual(transcripts.read(self.current, "captain")["entries"], [])


class SupervisorTranscriptMethodTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(settings / "settings.toml", "[supervisor]\nrequest_timeout=5.0\n")
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: self.sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as ready:
            self.ready = json.loads(ready.readline())

    def call(self, method, params):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(self.ready["socket"])
            params = {"token": self.ready["token"], **params}
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            client.sendall(json.dumps(request).encode() + b"\n")
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + 10)

    def test_transcript_and_step_read_a_crew_that_joined_after_startup_redacted(self):
        record = {"id": "will", "incarnation_id": INCARNATION, "provider": "claude"}
        with sessions.crew_meta(self.current.directory) as meta:
            meta["crew"]["will"] = record
        events = self.current.events(f"will-{INCARNATION}")
        transcript = self.root / "native" / "projects" / "p" / "s.jsonl"
        lines(events, [{"hook_event_name": "SessionStart", "transcript_path": str(transcript)}])
        tool = {"type": "tool_use", "id": "t", "name": "Login", "input": {"apiKey": "sk-live"}}
        message = {"role": "assistant", "content": [tool]}
        lines(transcript, [{"type": "assistant", "message": message}])
        page = self.call("transcript", {"agent": "will", "limit": 10})["result"]
        self.assertEqual(page["entries"][0]["text"], '{"apiKey": "[redacted]"}')
        entry = self.call("step", {"agent": "will", "id": page["entries"][0]["id"]})["result"]
        self.assertNotIn("sk-live", json.dumps(entry))
        error = self.call("transcript", {"agent": "will", "after": "nope"})["error"]
        self.assertEqual(error["data"], {"code": "transcript-range"})


if __name__ == "__main__":
    unittest.main()
