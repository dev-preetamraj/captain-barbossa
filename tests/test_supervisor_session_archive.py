"""archive_session, unarchive_session and delete_session over the supervisor socket: another stopped
session is archived or deleted in place, and archiving this one ends and stops its supervisor."""

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, jsonrpc, sessions, store, supervision, supervisor


class SupervisorSessionArchiveTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=5.0\n",
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
        self.own = supervisor.open_session(self.project, None)
        self.other = supervisor.open_session(self.project, None)
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.own, out, stopped)

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            self.ready = json.loads(lines.readline())

    def call(self, method, params=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(30)
            client.connect(self.ready["socket"])
            params = {"token": self.ready["token"], **(params or {})}
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            client.sendall(json.dumps(request).encode() + b"\n")
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + 30)

    def listed(self, params=None):
        listing = self.call("sessions", params)["result"]
        return {row["id"]: row for project in listing["projects"] for row in project["sessions"]}

    def test_another_session_is_archived_shown_with_all_and_unarchived(self):
        other = self.other.meta["id"]
        reply = self.call("archive_session", {"session_id": other})
        self.assertIs(reply["result"]["stopped"], False)
        self.assertNotIn(other, self.listed())
        self.assertTrue(self.listed({"all": True})[other]["archived"])
        reply = self.call("unarchive_session", {"session_id": other})
        self.assertFalse(reply["result"]["archived"])
        self.assertFalse(self.listed()[other]["archived"])

    def test_delete_refuses_this_session_and_unknown_ids_and_logs_on_this_stream(self):
        own, other = self.own.meta["id"], self.other.meta["id"]
        refused = self.call("delete_session", {"session_id": own})
        self.assertEqual(refused["error"]["data"]["code"], "supervisor-running")
        for unknown in ("f" * 32, "not-an-id"):
            refused = self.call("delete_session", {"session_id": unknown})
            self.assertEqual(refused["error"]["data"]["code"], "session-unknown")
        reply = self.call("delete_session", {"session_id": other})
        self.assertEqual(reply["result"], {"session_id": other, "deleted": True})
        self.assertFalse(self.other.directory.exists())
        actions = (self.own.directory / "actions.jsonl").read_text().splitlines()
        logged = [(row["kind"], row.get("session_id")) for row in map(json.loads, actions)]
        self.assertIn(("session-deleted", other), logged)

    def test_archiving_this_session_ends_it_and_stops_the_supervisor(self):
        own = self.own.meta["id"]
        reply = self.call("archive_session", {"session_id": own})
        self.assertIs(reply["result"]["stopped"], True)
        self.thread.join(30)
        self.assertFalse(self.thread.is_alive())
        meta = store.read_json(self.own.meta_path)
        self.assertTrue(meta["archived_at"] and meta["ended_at"])
        listed = sessions.catalog(self.project)
        self.assertNotIn(own, [row["id"] for p in listed["projects"] for row in p["sessions"]])
