"""checkout_diff: the project checkout's working-tree or staged diff, through hardened Git."""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, safe_read, store, supervision, supervisor
from captain_barbossa.runtime import CaptainError


class CheckoutDiffTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(settings / "settings.toml", "[supervisor]\nrequest_timeout=5.0\n")
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
        self.git("init", "-q", "--template=", "-b", "main")
        (self.project / "file").write_text("original\n")
        self.git("add", "file")
        commit = ("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "i")
        self.git(*commit)
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
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]

    def git(self, *args):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "GIT_CONFIG_GLOBAL": "/dev/null"}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        subprocess.run([str(safe_read.GIT), *args], cwd=self.project, env=env, check=True)

    def call(self, params):
        return supervision.request(self.current.directory, "checkout_diff", params, timeout=30)

    def test_working_and_staged_scopes_show_their_own_changes(self):
        (self.project / "file").write_text("changed\n")
        working = self.call({})
        self.assertEqual((working["scope"], working["root"]), ("working", str(self.project)))
        self.assertIn("+changed", working["diff"])
        self.assertIn({"index": " ", "worktree": "M", "path": "file"}, working["files"])
        self.assertEqual(self.call({"scope": "staged"})["diff"], "")
        self.git("add", "file")
        self.assertIn("+changed", self.call({"scope": "staged"})["diff"])
        self.assertEqual(self.call({"scope": "working"})["diff"], "")

    def test_a_bad_scope_or_an_unreadable_checkout_is_a_typed_refusal(self):
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-params\]"):
            self.call({"scope": "everything"})
        shutil.rmtree(self.project / ".git")
        with self.assertRaisesRegex(CaptainError, r"\[checkout-diff\]"):
            self.call({})


if __name__ == "__main__":
    unittest.main()
