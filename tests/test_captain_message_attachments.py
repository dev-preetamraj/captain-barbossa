"""Files attached to the user's message for the captain are checked when it is queued, recorded
on the prompt and its transcript entry, and reach each provider as native images or paths."""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    attachments,
    captain_bridge,
    claude_agent,
    claude_host,
    codex_app_server,
    config,
    eventlog,
    pi_rpc,
    store,
    supervision,
    supervisor,
    transcripts,
)
from captain_barbossa.runtime import CaptainError

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 16
SESSION = "0a1b2c3d-1111-4222-8333-444455556666"
NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


class FakePeer:
    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def request(self, method, params, timeout):
        self.requests.append((method, params))
        return self.answers.get(method, {})


def link(answers, **fields):
    return SimpleNamespace(
        guard=threading.Lock(),
        native={},
        turn=None,
        thread="t1",
        model=None,
        writable=(),
        captain=True,
        peer=FakePeer(answers),
        **fields,
    )


class CaptainMessageAttachmentTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        self.directory = self.current.directory
        self.files = self.root / "files"
        self.files.mkdir()
        self.native = self.root / "native"
        os.environ.update(CLAUDE_CONFIG_DIR=str(self.native))

    def write(self, name, data):
        path = self.files / name
        path.write_bytes(data)
        return str(path)

    def records(self):
        return attachments.validate(
            [{"path": self.write("shot.png", PNG)}, {"path": self.write("notes.md", b"# N\n")}]
        )

    def actions(self):
        eventlog.flush_actions(self.directory)
        rows, _ = eventlog.read_events(self.directory / eventlog.ACTIONS, 0)
        return [row for row in rows if row["kind"] == "prompt"]

    def test_an_image_is_known_by_its_own_bytes_and_any_other_file_is_a_file(self):
        shot = self.write("shot.dat", PNG)
        notes = self.write("notes.md", b"# Notes\n")
        sticker = self.write("sticker.webp", WEBP)
        found = attachments.validate(
            [{"path": shot}, {"path": notes, "name": "Notes"}, {"path": sticker, "mime": "x/y"}]
        )
        self.assertEqual(
            found,
            [
                {
                    "path": shot,
                    "name": "shot.dat",
                    "mime": "image/png",
                    "kind": "image",
                    "size": 40,
                },
                {
                    "path": notes,
                    "name": "Notes",
                    "mime": "text/markdown",
                    "kind": "file",
                    "size": 8,
                },
                {
                    "path": sticker,
                    "name": "sticker.webp",
                    "mime": "image/webp",
                    "kind": "image",
                    "size": 32,
                },
            ],
        )
        self.assertEqual(attachments.validate(None), [])

    def test_bad_attachments_are_refused_with_their_code(self):
        png = self.write("shot.png", PNG)
        os.symlink(png, self.files / "link.png")
        large = self.write("large.png", PNG)
        os.truncate(large, attachments.IMAGE_BYTES + 1)
        log = self.write("big.log", b"x")
        os.truncate(log, attachments.IMAGE_BYTES * 2)
        cases = (
            ([{"path": "shot.png"}], "attachment-missing"),
            ([{"path": str(self.files / "gone.png")}], "attachment-missing"),
            ([{"path": str(self.files)}], "attachment-unsupported"),
            ([{"path": str(self.files / "link.png")}], "attachment-unsupported"),
            ([{"path": png}] * (attachments.LIMIT + 1), "attachment-unsupported"),
            ({"path": png}, "attachment-unsupported"),
            ([{"path": png, "name": 3}], "attachment-unsupported"),
            ([{"path": large}], "attachment-too-large"),
        )
        for raw, code in cases:
            with self.subTest(code=code, raw=str(raw)[:80]):
                with self.assertRaisesRegex(CaptainError, rf"^\[{code}\]"):
                    attachments.validate(raw)
        self.assertEqual(attachments.validate([{"path": log}])[0]["kind"], "file")

    def test_a_queued_message_keeps_its_attachments_and_its_id_refuses_other_files(self):
        records = self.records()
        raw = [{"path": record["path"]} for record in records]
        self.assertTrue(captain_bridge.queue_message(self.directory, "m1", "Look", raw))
        self.assertFalse(captain_bridge.queue_message(self.directory, "m1", "Look", raw))
        with self.assertRaisesRegex(CaptainError, r"^\[captain-message-id\]"):
            captain_bridge.queue_message(self.directory, "m1", "Look", raw[:1])
        with self.assertRaisesRegex(CaptainError, r"^\[attachment-missing\]"):
            captain_bridge.queue_message(self.directory, "m2", "Look", [{"path": "x"}])
        rows = captain_bridge._messages(self.directory)
        self.assertEqual([(row["id"], row["attachments"]) for row in rows], [("m1", records)])
        self.assertEqual([row["attachments"] for row in self.actions()], [records])

    def test_sending_rings_the_attachments_and_records_them_delivered(self):
        records = self.records()
        captain_bridge.queue_message(
            self.directory, "m1", "Look", [{"path": record["path"]} for record in records]
        )
        captain_bridge.queue_message(self.directory, "m2", "Plain")
        rings = []

        def ring(delivery_id, text, *attached):
            rings.append((delivery_id, text, *attached))
            return {"landed": True, "gate": None}

        self.assertTrue(captain_bridge.send_messages(self.directory, ring, set(), None))
        self.assertEqual(rings, [("user-m1", "Look", records), ("user-m2", "Plain")])
        self.assertEqual(
            [(row["message"], row["state"], row["attachments"]) for row in self.actions()],
            [("m1", "queued", records), ("m2", "queued", []), ("m1", "delivered", records)]
            + [("m2", "delivered", [])],
        )

    def test_images_go_natively_and_other_files_unreadable_or_over_budget_go_by_path(self):
        image, notes = self.records()
        text, images = attachments.prompt("Look", [image, notes])
        reference = f"- {notes['path']} (notes.md, text/markdown)"
        self.assertEqual(
            (text, images), ("Look" + attachments.REFERENCES_HEAD + reference, [(image, PNG)])
        )
        self.assertEqual(attachments.without_references(text), "Look")
        text, images = attachments.prompt("Look", [image], budget=0)
        self.assertEqual((text.endswith("(shot.png, image/png)"), images), (True, []))
        Path(image["path"]).write_bytes(b"not a png any more")
        self.assertEqual(attachments.prompt("Look", [image])[1], [])
        self.assertEqual(attachments.prompt("Look", []), ("Look", []))

    def test_codex_starts_the_turn_with_a_local_image_item(self):
        image, notes = self.records()
        codex = link({"turn/start": {"turn": {"id": "turn1"}}})
        self.assertTrue(codex_app_server.ring(codex, "user-m1", "Look", [image, notes])["landed"])
        method, params = codex.peer.requests[0]
        text, _ = attachments.prompt("Look", [notes])
        self.assertEqual(method, "turn/start")
        self.assertEqual(
            params["input"],
            [{"type": "text", "text": text}, {"type": "localImage", "path": image["path"]}],
        )
        self.assertEqual(codex.native["inputs"], [text])

    def test_pi_prompts_with_base64_images(self):
        image, notes = self.records()
        pi = link({"get_state": {"isStreaming": False}, "prompt": {"disposition": "started"}})
        self.assertTrue(pi_rpc.ring(pi, "user-m1", "Look", [image, notes])["landed"])
        method, params = pi.peer.requests[-1]
        self.assertEqual(method, "prompt")
        self.assertEqual(params["message"], attachments.prompt("Look", [notes])[0])
        self.assertEqual(
            params["images"],
            [{"type": "image", "data": attachments.encoded(PNG), "mimeType": "image/png"}],
        )
        plain = link({"get_state": {"isStreaming": False}, "prompt": {"disposition": "started"}})
        pi_rpc.ring(plain, "user-m2", "Plain")
        self.assertEqual(plain.peer.requests[-1], ("prompt", {"message": "Plain"}))

    def test_a_claude_captain_query_carries_attachments_the_host_streams_as_image_blocks(self):
        image, notes = self.records()
        claude = link({})
        claude_agent.ring(claude, "user-m1", "Look", [image, notes])
        self.assertEqual(
            claude.peer.requests, [("query", {"text": "Look", "attachments": [image, notes]})]
        )

        async def collect(prompt):
            return [message async for message in prompt]

        (message,) = asyncio.run(collect(claude_host.user_prompt("Look", [image, notes])))
        content = message["message"]["content"]
        self.assertEqual(
            content,
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": attachments.encoded(PNG),
                    },
                },
                {"type": "text", "text": attachments.prompt("Look", [notes])[0]},
            ],
        )
        self.assertEqual(claude_host.user_prompt("Plain", []), "Plain")

    def test_the_captain_transcript_pairs_a_prompt_with_its_attachments(self):
        records = self.records()
        captain_bridge.queue_message(
            self.directory, "m1", "Look", [{"path": record["path"]} for record in records]
        )
        captain_bridge.message_delivered(self.directory, "m1")
        sent, _ = attachments.prompt("Look", records)
        transcript = self.native / "projects" / "p" / f"{SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        start = {"hook_event_name": "SessionStart", "transcript_path": str(transcript)}
        self.current.events("captain").parent.mkdir(parents=True, exist_ok=True)
        self.current.events("captain").write_text(
            json.dumps({**start, "session_id": SESSION}) + "\n", encoding="utf-8"
        )
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png"}}
        content = [image, {"type": "text", "text": sent}]
        records_out = [
            {"type": "user", "message": {"role": "user", "content": "<context>cwd</context>"}},
            {"type": "user", "message": {"role": "user", "content": content}},
        ]
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records_out), encoding="utf-8"
        )
        entries = transcripts.read(self.current, "captain")["entries"]
        self.assertEqual(
            [(e["origin"], e["message"], e["text"], e["attachments"]) for e in entries],
            [("native", None, "<context>cwd</context>", []), ("user", "m1", "Look", records)],
        )
        whole = transcripts.step(self.current, "captain", entries[1]["id"])
        self.assertEqual(whole["attachments"], records)

    def test_codex_image_wrappers_are_not_part_of_the_prompt_text(self):
        content = [
            {"type": "input_text", "text": "Look"},
            {"type": "input_text", "text": "<image name=[Image #1]>"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            {"type": "input_text", "text": "</image>"},
        ]
        record = {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": content},
        }
        self.assertEqual([block["text"] for block in transcripts._codex(record)], ["Look"])


class SupervisorMessageAttachmentTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=2.0\n",
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(patch.object(supervisor, "BRIDGE_POLL", 0.05))
        self.rung, self.busy = [], threading.Event()

        def ring(link, message_id, *sent):
            if self.busy.is_set():
                return {"landed": False, "gate": "agent not idle"}
            self.rung.append((message_id, *sent))
            return {"landed": True, "gate": None}

        adapter = SimpleNamespace(
            open=lambda link: setattr(link, "thread", link.thread or "thread-1"),
            state=lambda link: "working" if self.busy.is_set() else "idle",
            ring=ring,
            interrupt=lambda link, reason: "interrupted",
        )
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": adapter}))
        self.enterContext(
            patch.object(
                supervisor,
                "captain_command",
                side_effect=lambda sup, provider, model, effort: (
                    NATIVE,
                    dict(os.environ),
                    model,
                    "rpc",
                ),
            )
        )
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
        with os.fdopen(read_fd) as lines:
            ready = json.loads(lines.readline())
        os.environ[supervision.TOKEN_ENV] = ready["token"]
        self.shot = self.root / "shot.png"
        self.shot.write_bytes(PNG)

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def test_message_captain_queues_attachments_lists_them_and_rings_them_once_idle(self):
        self.call("start_captain", {"provider": "fake"})
        self.busy.set()
        attached = [{"path": str(self.shot), "name": "Shot"}]
        params = {"text": "Look.", "message_id": "m1", "attachments": attached}
        self.assertEqual(self.call("message_captain", params)["state"], "queued")
        record = {"path": str(self.shot), "name": "Shot", "mime": "image/png"}
        record.update(kind="image", size=len(PNG))
        (prompt,) = self.call("snapshot")["prompts"]
        self.assertEqual((prompt["id"], prompt["attachments"]), ("m1", [record]))
        self.busy.clear()
        deadline = time.monotonic() + 10
        while not self.rung and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.rung, [("user-m1", "Look.", [record])])
        self.call("message_captain", {"text": "Plain.", "message_id": "m2"})
        self.assertEqual(self.rung[-1], ("user-m2", "Plain."))

    def test_message_captain_refuses_bad_attachments_with_their_code(self):
        self.call("start_captain", {"provider": "fake"})
        cases = (
            ([{"path": "shot.png"}], "attachment-missing"),
            ([{"path": str(self.root)}], "attachment-unsupported"),
            ([{"path": str(self.shot)}] * 11, "attachment-unsupported"),
        )
        for attached, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(CaptainError, rf"\[{code}\]"):
                self.call("message_captain", {"text": "Look.", "attachments": attached})
        self.assertEqual(self.rung, [])
