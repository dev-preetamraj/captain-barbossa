"""Native hook commands append event lines, and the event reader tolerates bad ones."""

import contextlib
import io
import json
import os
import subprocess
import tomllib
import unittest
from unittest.mock import patch

from captain_barbossa import events, sessions, store
from captain_barbossa import instructions as instruction_prompts
from tests.home_isolation import HERDR, SessionCase


class NativeEventLogTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def event_path(self):
        return store.private_dir(self.directory / "events") / "jack.jsonl"

    def test_native_hook_commands_append_one_json_line_from_stdin_or_argv(self):
        events = self.root / "events with 'quotes' $() and spaces.jsonl"
        expected = []
        for provider in ("claude", "codex"):
            args = instruction_prompts.native_args(provider, "instructions", events=events)
            if provider == "claude":
                settings = json.loads(args[args.index("--settings") + 1])
                self.assertEqual(
                    settings["attribution"],
                    json.loads(instruction_prompts.CLAUDE_NO_ATTRIBUTION)["attribution"],
                )
                self.assertEqual(
                    set(settings["hooks"]),
                    {
                        "SessionStart",
                        "UserPromptSubmit",
                        "Stop",
                        "Notification",
                        "PermissionRequest",
                        "PreToolUse",
                        "PostToolUse",
                        "PostToolUseFailure",
                    },
                )
                for kind, entries in settings["hooks"].items():
                    event = {"hook_event_name": kind, "message": "quotes '\"\n雪"}
                    if kind == "PreToolUse":
                        # Claude always names the tool; with no crew state the native mode decides.
                        event |= {"tool_name": "Bash", "tool_input": {"command": "ls"}}
                    command = entries[0]["hooks"][0]["command"]
                    result = subprocess.run(
                        ["/bin/sh", "-c", command],
                        input=json.dumps(event, indent=2),
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    if kind == "PermissionRequest":
                        event["approval"] = {"verdict": "defer", "code": "unparsed"}
                    elif kind == "PreToolUse":
                        event["approval"] = {
                            "verdict": "defer",
                            "code": "unparsed",
                            "source": "PreToolUse",
                        }
                        event["hook_event_name"] = "PermissionDecision"
                        event["status"] = "working: approval defer [unparsed]"
                    expected.append(event)
            else:
                command = tomllib.loads(next(arg for arg in args if arg.startswith("notify=")))[
                    "notify"
                ]
                event = {"type": "agent-turn-complete", "last-assistant-message": "done\nOK"}
                result = subprocess.run(
                    [*command, json.dumps(event)], text=True, capture_output=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected.append(event)
        records = [json.loads(line) for line in events.read_text().splitlines()]
        for record in records:
            self.assertTrue(record.pop("timestamp"))
        self.assertEqual(records, expected)

    def test_event_reader_skips_bad_records_and_retries_a_partial_unicode_line(self):
        log = self.event_path()
        self.assertEqual(events.read_events(log, 0), ([], 0))
        prefix = b"invalid\n[]\nnull\n\xff\n"
        event = {"hook_event_name": "Stop", "message": "雪"}
        encoded = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        cut = encoded.index("雪".encode()) + 1
        log.write_bytes(prefix + encoded[:cut])
        self.assertEqual(events.read_events(log, 0), ([], len(prefix)))
        with log.open("ab") as file:
            file.write(encoded[cut:])
        self.assertEqual(events.read_events(log, len(prefix)), ([event], log.stat().st_size))


if __name__ == "__main__":
    unittest.main()
