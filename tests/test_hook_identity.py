"""Hook events must resolve to the exact crew incarnation, even when crew names share a prefix."""

import unittest

from captain_barbossa import events
from captain_barbossa.runtime import CaptainError
from captain_barbossa.store import write_json
from tests.home_isolation import SessionCase


class HookIdentityTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.directory = self.root / "session"
        (self.directory / "events").mkdir(parents=True)
        write_json(
            self.directory / "session.json",
            {
                "crew": {
                    "jack": {"incarnation_id": "aaa"},
                    "jack-1": {"incarnation_id": "bbb"},
                }
            },
        )
        # jack-1 is listed first so a first-prefix match would pick it for jack's hook.
        write_json(
            self.directory / "protocol.json",
            {
                "active": {"jack-1": "as2", "jack": "as1"},
                "assignments": {
                    "as1": {
                        "id": "as1",
                        "crew": "jack",
                        "incarnation_id": "aaa",
                        "state": "active",
                    },
                    "as2": {
                        "id": "as2",
                        "crew": "jack-1",
                        "incarnation_id": "bbb",
                        "state": "active",
                    },
                },
            },
        )

    def path(self, stem):
        return self.directory / "events" / f"{stem}.jsonl"

    def test_numbered_crew_resolves_to_itself_not_its_prefix(self):
        directory, crew_id, assignment = events.crew_for_events(self.path("jack-1-bbb"))
        self.assertEqual((directory, crew_id), (self.directory, "jack-1"))
        self.assertEqual(assignment["id"], "as2")
        self.assertEqual(events.event_incarnation(self.path("jack-1-bbb")), "bbb")

    def test_prefix_crew_still_resolves_with_numbered_sibling(self):
        _, crew_id, assignment = events.crew_for_events(self.path("jack-aaa"))
        self.assertEqual(crew_id, "jack")
        self.assertEqual(assignment["id"], "as1")
        self.assertEqual(events.event_incarnation(self.path("jack-aaa")), "aaa")

    def test_wrong_incarnation_of_numbered_crew_is_stale(self):
        with self.assertRaisesRegex(CaptainError, "Stale crew incarnation"):
            events.crew_for_events(self.path("jack-1-aaa"))
        with self.assertRaisesRegex(CaptainError, "Stale crew incarnation"):
            events.event_incarnation(self.path("jack-1-aaa"))

    def test_cached_approval_requires_complete_matching_call_context(self):
        event = {
            "session_id": "native",
            "tool_use_id": "call",
            "cwd": "/project",
            "incarnation_id": "aaa",
            "tool_name": "Edit",
            "tool_input": {"file_path": "relative"},
        }
        prior = {**event, "approval": {"source": "PreToolUse", "code": "granted"}}
        self.assertIs(events.pretool_decision([prior], event), prior)
        for key in event:
            for value in (None, "different"):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(events.pretool_decision([prior], {**event, key: value}))
        consumed = {"approval": {"pretool": "call"}}
        self.assertIsNone(events.pretool_decision([prior, consumed], event))

    def test_unknown_crew_path_is_refused(self):
        with self.assertRaisesRegex(CaptainError, "Unknown crew event path"):
            events.event_incarnation(self.path("zed-ccc"))


if __name__ == "__main__":
    unittest.main()
