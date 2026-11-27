"""Captain transcript deliveries expose their recorded crew and assignment identity."""

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


class TranscriptDeliveryFieldTests(unittest.TestCase):
    def test_delivery_fields_come_from_the_claim_and_other_entries_use_nulls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            history = directory / "history.jsonl"
            delivery = {
                "crew": "Will",
                "status": "asked",
                "assignment_id": "assignment-1",
                "delivery_id": "delivery-1",
                "question_id": "question-1",
                "summary": "Which path?",
            }
            captain_bridge.claim(directory, delivery, set())
            captain_bridge.delivered(directory, delivery)
            records = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": captain_bridge.render(delivery, directory),
                    },
                },
                {"type": "assistant", "message": {"role": "assistant", "content": "Checking."}},
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
                page = transcripts.read(session, "captain")
                whole = transcripts.step(session, "captain", page["entries"][0]["id"])

            expected = {
                "crew": "Will",
                "status": "asked",
                "assignment_id": "assignment-1",
                "delivery_id": "delivery-1",
                "question_id": "question-1",
            }
            self.assertEqual({key: page["entries"][0][key] for key in expected}, expected)
            self.assertEqual({key: whole[key] for key in expected}, expected)
            self.assertEqual(
                {key: page["entries"][1][key] for key in expected},
                dict.fromkeys(expected),
            )


if __name__ == "__main__":
    unittest.main()
