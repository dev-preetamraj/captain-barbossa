"""Plugin mirror: wholesale generations, kept on failure, pinned by sessions, collected."""

import json

from captain_barbossa import plugins, store
from tests.home_isolation import SessionCase
from tests.plugin_fixture import FakePlugin, register, snapshot, unregister


class MirrorPublicationTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(unregister)
        self.plugin = FakePlugin(self.root)
        register(self.plugin)
        self.mirror = plugins.directory("example")

    def generations(self):
        return sorted(path.name for path in (self.mirror / "generations").iterdir())

    def files(self, generation):
        root = self.mirror / "generations" / generation
        return {
            str(path.relative_to(root)): path.read_text()
            for path in root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }

    def test_publication_replaces_the_whole_snapshot(self):
        first = plugins.sync("example")
        self.assertEqual(
            self.files(first),
            {"context/captain.md": "captain context\n", "context/crew.md": "crew context\n"},
        )
        self.plugin.script(sync=snapshot(captain="new\n", crew=None))
        second = plugins.sync("example")
        self.assertNotEqual(first, second)
        self.assertEqual(self.files(second), {"context/captain.md": "new\n"})
        pointer = plugins.pointer("example")
        self.assertEqual(pointer["generation"], second)
        self.assertEqual(pointer["server_id"], "host")
        self.assertEqual(self.generations(), [second])

    def test_failure_keeps_the_current_generation_and_records_why(self):
        current = plugins.sync("example")
        for reply, code in (
            ({"ok": False, "error": {"code": "login_required", "message": "x"}}, "login_required"),
            ({"_exit": 3}, "plugin_exit"),
            ({"_raw": "not json"}, "bad_response"),
            ({"ok": True}, "invalid_manifest"),
            (
                {**snapshot(), "files": [{"path": "../x", "roles": ["crew"], "text": ""}]},
                "invalid_manifest",
            ),
            ({**snapshot(), "expires_at": "tomorrow"}, "invalid_manifest"),
            ({**snapshot(), "server_id": ""}, "invalid_manifest"),
            (snapshot(crew="bell\x07"), "invalid_context"),
            (snapshot(crew="x" * plugins.PLUGIN_CEILING), "context_too_large"),
        ):
            with self.subTest(code=code, reply=str(reply)[:60]):
                self.plugin.script(sync=reply)
                with self.assertRaises(plugins.PluginError) as caught:
                    plugins.sync("example")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(plugins.pointer("example")["generation"], current)
                self.assertEqual(self.generations(), [current])
                self.assertEqual(store.read_json(self.mirror / "status.json")["error"], code)
                self.assertEqual([p.name for p in self.mirror.glob(".tmp-*")], [])

    def test_a_missed_deadline_is_a_timeout_failure(self):
        current = plugins.sync("example")
        self.plugin.script(sync={**snapshot(), "_sleep": 5})
        self.enterContext(
            __import__("unittest.mock").mock.patch.dict(plugins.DEADLINES, {"sync": 0.5})
        )
        with self.assertRaises(plugins.PluginError) as caught:
            plugins.sync("example")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(plugins.pointer("example")["generation"], current)

    def test_a_held_lock_skips_the_sync(self):
        with store.lock(self.mirror / "lock"):
            with self.assertRaises(plugins.PluginError) as caught:
                plugins.sync("example")
        self.assertEqual(caught.exception.code, "locked")
        self.assertEqual(self.plugin.requests(), [])

    def test_crash_leftovers_are_collected_and_never_served(self):
        current = plugins.sync("example")
        (self.mirror / ".tmp-dead").mkdir()
        (self.mirror / "generations" / "0-orphan").mkdir()
        latest = plugins.sync("example")
        self.assertNotEqual(current, latest)
        self.assertEqual(self.generations(), [latest])
        self.assertEqual(list(self.mirror.glob(".tmp-*")), [])

    def test_pinned_generations_survive_until_their_session_is_gone(self):
        session = self.root / "session-a"
        session.mkdir()
        pinned = plugins.sync("example")
        plugins.pin("example", pinned, session)
        middle = plugins.sync("example")
        latest = plugins.sync("example")
        self.assertEqual(self.generations(), sorted([pinned, latest]))
        self.assertNotIn(middle, self.generations())
        session.rmdir()
        plugins.sync("example")
        self.assertNotIn(pinned, self.generations())
        self.assertEqual(list((self.mirror / "refs").iterdir()), [])

    def test_pin_retries_when_collection_won_the_race(self):
        session = self.root / "session-b"
        session.mkdir()
        gone = plugins.sync("example")
        latest = plugins.sync("example")
        self.assertEqual(plugins.pin("example", gone, session), latest)

    def test_an_empty_generation_is_current_and_injects_nothing(self):
        plugins.sync("example")
        self.plugin.script(sync={**snapshot(), "files": []})
        wipe = plugins.sync("example")
        self.assertEqual(plugins.pointer("example")["generation"], wipe)
        self.assertEqual(self.generations(), [wipe])
        self.assertEqual(plugins.section("example", wipe, "captain"), "")

    def test_server_id_is_opaque_and_returned_on_the_next_sync(self):
        plugins.sync("example")
        self.plugin.script(sync=snapshot(server="https://other.example/a b?c"))
        plugins.sync("example")
        self.assertEqual(plugins.pointer("example")["server_id"], "https://other.example/a b?c")
        requests = [r for r in self.plugin.requests() if r["call"] == "sync"]
        self.assertEqual([r["server_id"] for r in requests], [None, "host"])

    def test_status_reports_freshness_without_calling_the_plugin(self):
        plugins.sync("example")
        calls = len(self.plugin.requests())
        report = plugins.status("example")
        self.assertEqual(report["server_id"], "host")
        self.assertFalse(report["stale"])
        self.assertFalse(report["expired"])
        self.assertIsNone(report["error"])
        self.assertEqual(
            report["bytes"]["captain"],
            len(plugins.section("example", report["generation"], "captain").encode()),
        )
        self.assertEqual(len(self.plugin.requests()), calls)
        json.dumps(report)
