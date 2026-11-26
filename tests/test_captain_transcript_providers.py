"""A captain transcript holds every provider's conversation in order with a boundary at each switch,
and the context a native injects as a user message is labelled, never taken for a prompt."""

import contextlib
import json
import os
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import captain_bridge, supervision, supervisor, transcripts

CLAUDE = "0a1b2c3d-1111-4222-8333-444455556666"
THREAD = "01a2176a-48b1-70c0-a026-b307ee836b13"
PI = "01a1e34e-441a-71cf-8437-5dfd6820116f"


def lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record) + "\n")


def codex_message(role, text):
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    payload = {"type": "message", "role": role, "content": content}
    return {"type": "response_item", "payload": payload}


class CaptainTranscriptProviderTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        self.directory = self.current.directory
        self.native = self.root / "native"
        os.environ.update(
            CLAUDE_CONFIG_DIR=str(self.native),
            CODEX_HOME=str(self.native),
            PI_CODING_AGENT_DIR=str(self.native / "pi"),
        )
        os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        self.claude_file = self.native / "projects" / "p" / f"{CLAUDE}.jsonl"
        self.rollout = self.native / "sessions" / "2026" / "11" / "26" / f"rollout-x-{THREAD}.jsonl"

    def prompt(self, message_id, text):
        captain_bridge.queue_message(self.directory, message_id, text)
        captain_bridge.message_delivered(self.directory, message_id)

    def events(self, *records):
        lines(self.current.events("captain"), records)

    def claude_session(self):
        start = {"hook_event_name": "SessionStart", "session_id": CLAUDE, "provider": "claude"}
        self.events({**start, "transcript_path": str(self.claude_file)})
        self.prompt("m1", "Plan it.")
        lines(
            self.claude_file,
            [
                {"type": "user", "message": {"role": "user", "content": "Plan it."}},
                {"type": "assistant", "message": {"role": "assistant", "content": "Planned."}},
            ],
        )

    def codex_session(self, write=True):
        start = {"hook_event_name": "SessionStart", "session_id": THREAD, "provider": "codex"}
        self.events(
            {**start, "transcript_path": str(self.rollout)},
            {"type": "agent-turn-complete", "thread-id": THREAD, "provider": "codex"},
        )
        self.prompt("m2", "Ship it.")
        if write:
            lines(
                self.rollout,
                [
                    {"type": "session_meta", "payload": {"id": THREAD}},
                    codex_message("user", "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>x"),
                    codex_message("user", "<environment_context>cwd</environment_context>"),
                    codex_message("user", "Ship it."),
                    codex_message("assistant", "Shipped."),
                ],
            )

    def shape(self, entries):
        return [
            (e["provider"], e["role"], e["origin"], e["label"], e["message"], e["text"])
            for e in entries
        ]

    def test_a_provider_switch_returns_both_conversations_in_order_with_a_boundary(self):
        self.claude_session()
        self.codex_session()
        page = transcripts.read(self.current, "captain")
        self.assertEqual(page["provider"], "codex")
        self.assertEqual(page["files"], [str(self.claude_file), str(self.rollout.resolve())])
        switch = "Provider switched from claude to codex."
        self.assertEqual(
            self.shape(page["entries"]),
            [
                ("claude", "user", "user", None, "m1", "Plan it."),
                ("claude", "assistant", None, None, None, "Planned."),
                ("codex", "system", "provider-switch", None, None, switch),
                (
                    "codex",
                    "user",
                    "context",
                    "AGENTS.md instructions",
                    None,
                    page["entries"][3]["text"],
                ),
                (
                    "codex",
                    "user",
                    "context",
                    "Environment context",
                    None,
                    page["entries"][4]["text"],
                ),
                ("codex", "user", "user", None, "m2", "Ship it."),
                ("codex", "assistant", None, None, None, "Shipped."),
            ],
        )
        boundary = page["entries"][2]
        self.assertEqual(
            (boundary["id"], boundary["from"], boundary["to"]), ("1:0:0", "claude", "codex")
        )
        self.assertEqual(transcripts.step(self.current, "captain", boundary["id"])["to"], "codex")
        prompt = transcripts.step(self.current, "captain", page["entries"][5]["id"])
        self.assertEqual((prompt["provider"], prompt["message"]), ("codex", "m2"))
        older = transcripts.read(self.current, "captain", before=boundary["id"][:-2], limit=1)
        self.assertEqual([e["provider"] for e in older["entries"]], ["claude"])
        newer = transcripts.read(self.current, "captain", after=older["end"])
        self.assertEqual(self.shape(newer["entries"]), self.shape(page["entries"][2:]))

    def test_codex_context_reinjected_on_each_resume_takes_no_message_id(self):
        self.codex_session()
        agents = "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>x"
        environment = "<environment_context>cwd</environment_context>"
        self.prompt("m3", "Again.")
        resumed = [codex_message("user", agents), codex_message("user", environment)]
        lines(self.rollout, [*resumed, *resumed, codex_message("user", "Again.")])
        entries = transcripts.read(self.current, "captain")["entries"]
        users = [(e["origin"], e["label"], e["message"]) for e in entries if e["role"] == "user"]
        context = [
            ("context", "AGENTS.md instructions", None),
            ("context", "Environment context", None),
        ]
        self.assertEqual(
            users, [*context, ("user", None, "m2"), *context, *context, ("user", None, "m3")]
        )

    def test_a_conversation_not_yet_on_disk_keeps_its_index(self):
        self.claude_session()
        self.codex_session(write=False)
        page = transcripts.read(self.current, "captain")
        self.assertEqual(page["files"], [str(self.claude_file), None])
        self.assertEqual([e["provider"] for e in page["entries"]], ["claude", "claude"])
        meta = {"type": "session_meta"}
        lines(self.rollout, [meta, codex_message("user", "Ship it.")])
        found = transcripts.read(self.current, "captain", after=page["end"])
        second = f"1:{len(json.dumps(meta)) + 1}:0"
        self.assertEqual(
            [(e["id"], e["origin"], e["message"]) for e in found["entries"]],
            [("1:0:0", "provider-switch", None), (second, "user", "m2")],
        )

    def test_a_pi_captain_after_codex_is_found_by_its_session_id(self):
        self.codex_session()
        pi_file = self.native / "pi" / "sessions" / "--p--" / f"2026-11-16T07-41-59-706Z_{PI}.jsonl"
        self.events({"type": "agent-turn-complete", "thread-id": PI, "provider": "pi"})
        self.prompt("m3", "Again.")
        user = {"role": "user", "content": [{"type": "text", "text": "Again."}]}
        lines(pi_file, [{"type": "session"}, {"type": "message", "message": user}])
        entries = transcripts.read(self.current, "captain")["entries"]
        self.assertEqual(
            [(e["provider"], e["origin"], e["message"]) for e in entries[-2:]],
            [("pi", "provider-switch", None), ("pi", "user", "m3")],
        )

    def test_claude_injected_context_is_labelled_and_takes_no_message_id(self):
        start = {"hook_event_name": "SessionStart", "session_id": CLAUDE, "provider": "claude"}
        self.events({**start, "transcript_path": str(self.claude_file)})
        self.prompt("m1", "Go.")
        user = {"role": "user"}
        lines(
            self.claude_file,
            [
                {"type": "user", "isMeta": True, "message": {**user, "content": "Peer said hi."}},
                {"type": "user", "message": {**user, "content": "<task-notification>t"}},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {**user, "content": "This session is being continued."},
                },
                {"type": "user", "message": {**user, "content": "Go."}},
            ],
        )
        entries = transcripts.read(self.current, "captain")["entries"]
        self.assertEqual(
            [(e["origin"], e["label"], e["message"]) for e in entries],
            [
                ("context", "Claude context", None),
                ("context", "Task notification", None),
                ("context", "Compaction summary", None),
                ("user", None, "m1"),
            ],
        )
