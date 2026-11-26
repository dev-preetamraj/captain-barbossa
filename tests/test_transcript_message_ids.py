import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import captain_bridge, transcripts


class Session:
    def __init__(self, directory):
        self.directory = directory

    def events(self, agent):
        return self.directory / "events" / f"{agent}.jsonl"


class TranscriptMessageIdTests(unittest.TestCase):
    def test_delivered_prompts_match_duplicate_text_in_order_and_cli_prompt_is_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            history = directory / "history.jsonl"
            repeated = "same prompt"
            typed = "typed in the CLI"
            digest = hashlib.sha256(repeated.encode()).hexdigest()
            (directory / captain_bridge.PROMPTS).write_text(digest + "\n", encoding="utf-8")
            (directory / "protocol.json").write_text(
                json.dumps(
                    {
                        "captain_messages": [
                            {"id": "first", "digest": digest, "delivered_at": 1.0},
                            {"id": "second", "digest": digest, "delivered_at": 2.0},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            records = [
                {"type": "user", "message": {"role": "user", "content": repeated}},
                {"type": "user", "message": {"role": "user", "content": typed}},
                {"type": "user", "message": {"role": "user", "content": repeated}},
            ]
            history.write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            session = Session(directory)

            with patch.object(
                transcripts.usage,
                "captain_conversations",
                return_value=[("claude", str(history), None)],
            ):
                first = transcripts.read(session, "captain", after="0:0", limit=1)
                typed_page = transcripts.read(session, "captain", after=first["end"], limit=1)
                second = transcripts.read(session, "captain", after=typed_page["end"], limit=1)
                second_step = transcripts.step(session, "captain", second["entries"][0]["id"])

            self.assertEqual(
                (first["entries"][0]["origin"], first["entries"][0]["message"]),
                ("user", "first"),
            )
            self.assertEqual(
                (typed_page["entries"][0]["origin"], typed_page["entries"][0]["message"]),
                ("native", None),
            )
            self.assertEqual(
                (second["entries"][0]["origin"], second["entries"][0]["message"]),
                ("user", "second"),
            )
            self.assertEqual(second_step["message"], "second")


if __name__ == "__main__":
    unittest.main()
