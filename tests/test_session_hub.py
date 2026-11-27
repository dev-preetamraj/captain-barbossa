"""The session hub: every project's sessions listed from captain state with their run state, and
a supervised session renamed, ended and resumed through its own supervisor."""

import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    cli,
    config,
    jsonrpc,
    runtime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.runtime import CaptainError


class SessionHubTests(SessionCase):
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
        self.sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: self.sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)

    def serve(self, current):
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            return json.loads(lines.readline()), thread

    def call(self, ready, method, params=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(30)
            client.connect(ready["socket"])
            params = {"token": ready["token"], **(params or {})}
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            client.sendall(json.dumps(request).encode() + b"\n")
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + 30)

    def row(self, listing, session_id):
        return next(
            session
            for project in listing["projects"]
            for session in project["sessions"]
            if session["id"] == session_id
        )

    def test_every_project_and_session_is_listed_with_its_state(self):
        app = supervisor.open_session(self.project, None)
        herdr = sessions.session(self.project, self.pane, create=True)
        other_project = self.root / "other"
        other_project.mkdir()
        other = supervisor.open_session(other_project, None)
        listing = sessions.catalog(self.project)
        self.assertEqual(
            [project["project"] for project in listing["projects"]],
            [str(self.project), str(other_project)],
        )
        self.assertTrue(all(project["exists"] for project in listing["projects"]))
        self.assertEqual(
            {
                session["id"]: (session["transport"], session["state"])
                for project in listing["projects"]
                for session in project["sessions"]
            },
            {
                app.meta["id"]: ("supervisor", "stopped"),
                herdr.meta["id"]: ("herdr", "herdr"),
                other.meta["id"]: ("supervisor", "stopped"),
            },
        )
        ready, _ = self.serve(app)
        self.assertEqual(
            self.row(sessions.catalog(self.project), app.meta["id"])["state"], "running"
        )
        self.assertEqual(self.call(ready, "sessions")["result"], sessions.catalog(self.project))

    def test_a_session_is_renamed_ended_and_resumed_through_its_supervisor(self):
        app = supervisor.open_session(self.project, None)
        ready, thread = self.serve(app)
        renamed = self.call(ready, "rename", {"name": " Payments refactor "})["result"]
        self.assertEqual(renamed, {"session_id": app.meta["id"], "name": "Payments refactor"})
        error = self.call(ready, "rename", {"name": app.meta["id"]})["error"]
        self.assertEqual(error["data"], {"code": "session-name"})
        ended = self.call(ready, "shutdown", {"end": True})["result"]
        self.assertEqual(ended, {"crews": [], "ended": True})
        thread.join(30)
        row = self.row(sessions.catalog(self.project), app.meta["id"])
        self.assertEqual((row["name"], row["state"]), ("Payments refactor", "ended"))
        self.assertIsInstance(row["ended_at"], float)

        resumed = supervisor.open_session(self.project, app.meta["id"])
        self.assertNotIn("ended_at", resumed.meta)
        self.assertEqual(
            self.row(sessions.catalog(self.project), app.meta["id"])["state"], "stopped"
        )

    def test_captain_sessions_prints_the_list_with_no_herdr_and_no_supervisor(self):
        app = supervisor.open_session(self.project, None)
        herdr = sessions.session(self.project, self.pane, create=True)
        out = io.StringIO()
        no_herdr = CaptainError("Herdr must not be called.")
        with (
            patch.object(runtime, "herdr", side_effect=no_herdr),
            patch.object(cli, "current_pane", side_effect=no_herdr),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(cli.main(["sessions"]), 0)
        listing = json.loads(out.getvalue())
        self.assertEqual(listing, sessions.catalog(self.project))
        self.assertEqual(
            {row["id"] for row in listing["projects"][0]["sessions"]},
            {app.meta["id"], herdr.meta["id"]},
        )
        os.environ.update(CAPTAIN_ROLE="crew", CAPTAIN_CREW="will")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["sessions"]), 0)  # other projects are not crew's

    def test_quitting_the_app_stops_without_ending(self):
        app = supervisor.open_session(self.project, None)
        ready, thread = self.serve(app)
        self.assertEqual(self.call(ready, "shutdown")["result"], {"crews": [], "ended": False})
        thread.join(30)
        self.assertEqual(
            self.row(sessions.catalog(self.project), app.meta["id"])["state"], "stopped"
        )
        error_ready, _ = self.serve(app)
        error = self.call(error_ready, "shutdown", {"end": "yes"})["error"]
        self.assertEqual(error["data"], {"code": "supervisor-params"})


if __name__ == "__main__":
    unittest.main()
