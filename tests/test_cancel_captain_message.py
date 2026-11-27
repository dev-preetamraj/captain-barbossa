"""Queued captain prompts can be cancelled before delivery and their ids reused."""

import contextlib
import threading

from home_isolation import SessionCase

from captain_barbossa import captain_bridge, eventlog, store, supervisor


class CancelCaptainMessageTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)
        self.owner = supervisor.Supervisor(self.current, "user-token", threading.Event())

    def call(self, message_id, token=None):
        return supervisor.Handler.answer(
            self.owner,
            "cancel_captain_message",
            {"token": token or self.owner.token, "message_id": message_id},
        )

    def actions(self):
        eventlog.flush_actions(self.current.directory)
        return eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)[0]

    def test_a_queued_prompt_is_cancelled_recorded_and_its_id_can_be_reused(self):
        captain_bridge.queue_message(self.current.directory, "m1", "First.")

        self.assertEqual(self.call("m1")["result"], {"message_id": "m1", "state": "cancelled"})
        self.assertEqual(
            store.read_json(self.current.directory / "protocol.json")["captain_messages"], []
        )
        self.assertTrue(captain_bridge.queue_message(self.current.directory, "m1", "Replacement."))
        states = [row["state"] for row in self.actions() if row["kind"] == "prompt"]
        self.assertEqual(states, ["queued", "cancelled", "queued"])

    def test_delivered_uncertain_and_unknown_prompts_have_typed_refusals(self):
        directory = self.current.directory
        captain_bridge.queue_message(directory, "delivered", "Done.")
        captain_bridge.message_delivered(directory, "delivered")
        captain_bridge.queue_message(directory, "uncertain", "Maybe.")
        delivery = {"delivery_id": "user-uncertain", "message_id": "uncertain"}
        captain_bridge.claim(directory, delivery, set())

        for message_id, code in (
            ("delivered", "captain-message-delivered"),
            ("uncertain", "captain-message-uncertain"),
            ("missing", "captain-message-unknown"),
        ):
            with self.subTest(message_id=message_id):
                error = self.call(message_id)["error"]
                self.assertEqual(error["data"], {"code": code})

    def test_the_captain_scoped_token_cannot_cancel_the_users_prompt(self):
        captain_bridge.queue_message(self.current.directory, "m1", "Keep it.")

        error = self.call("m1", self.owner.captain_token)["error"]

        self.assertEqual(error["data"], {"code": "supervisor-captain-token"})


if __name__ == "__main__":
    import unittest

    unittest.main()
