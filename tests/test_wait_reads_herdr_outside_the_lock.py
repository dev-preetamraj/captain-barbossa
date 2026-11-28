"""A wait poll reads Herdr only while the session's protocol lock is free."""

import contextlib
import os
from unittest.mock import patch

from captain_barbossa import protocol, runtime, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase

# A Codex choice modal that Herdr reports idle, so every screen heuristic reads the pane.
MODAL = "\n".join(
    [
        "Approaching rate limits",
        "› 1. Switch to gpt-5.6-luna",
        "2. Keep current model",
        "Press enter to confirm or esc to go back",
    ]
)


class HerdrOutsideLockTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "pane": "w1:p2",
                "incarnation_id": "first",
            },
            current,
        )
        current.meta["crew"]["jack"] = self.crew.record
        store.write_json(current.meta_path, current.meta)
        protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def test_every_herdr_read_in_a_poll_happens_outside_the_protocol_lock(self):
        """Regression: the startup and modal screen reads ran inside checkpoint(), so one
        slow pane held every crew's ask, done and hooks behind its timeouts."""
        lock = self.crew.session.directory / "protocol.lock"
        reads, locked = [], []

        def herdr(*args, **kwargs):
            reads.append(args[:2])
            try:
                with store.lock(lock, timeout=0):
                    pass
            except CaptainError:
                locked.append(args)
            if args[:2] == ("pane", "get"):
                return {"pane": {"agent": "codex", "agent_status": "idle"}}
            return MODAL

        with patch.object(runtime, "herdr", side_effect=herdr):
            blocked = protocol.poll(self.crew)
        self.assertEqual(blocked["status"], "awaiting_approval")
        self.assertIn("Switch to gpt-5.6-luna", blocked["summary"])
        self.assertIn(("agent", "read"), reads)
        self.assertEqual(locked, [])
