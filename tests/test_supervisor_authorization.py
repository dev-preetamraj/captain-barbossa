"""Captain authorization: `do` and `quiet` reach the app's command surface only through an
authorization request the user resolves, and a supervised captain's outward or destructive
`do` runs only on an approved, one-shot request recorded in captain state."""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    config,
    do,
    eventlog,
    jsonrpc,
    processes,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.runtime import CaptainError


class CaptainAuthorizationTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        os.environ.pop(do.AUTHORIZATION_ENV, None)
        self.current = supervisor.open_session(self.project, None)
        self.directory = self.current.directory

    def rows(self):
        return store.read_json(self.directory / "protocol.json").get("authorizations", [])

    def test_an_outward_verb_opens_one_request_and_runs_once_it_is_approved(self):
        push = do.parse(["do", "push"])
        with self.assertRaisesRegex(CaptainError, r"\[authorization-required\].*outward"):
            do.authorize(push, self.directory)
        with self.assertRaisesRegex(CaptainError, r"\[authorization-required\]"):
            do.authorize(push, self.directory)  # a rerun waits on the same request
        (row,) = self.rows()
        self.assertEqual(
            (row["origin"], row["category"], row["state"]), ("captain", "outward", "open")
        )
        do.resolve_authorization(self.directory, row["id"], "approve")
        self.assertIsNone(do.authorize(push, self.directory))
        self.assertEqual(self.rows()[0]["state"], "spent")
        with self.assertRaisesRegex(CaptainError, r"\[authorization-required\]"):
            do.authorize(push, self.directory)  # one approval, one run
        self.assertEqual(len(self.rows()), 2)

    def test_local_verbs_run_without_a_request_and_destructive_ones_need_one(self):
        self.assertIsNone(do.authorize(do.parse(["do", "branch", "topic"]), self.directory))
        self.assertFalse((self.directory / "protocol.json").exists())
        with self.assertRaisesRegex(CaptainError, r"destructive"):
            do.authorize(do.parse(["do", "run", "deploy"]), self.directory)
        approved = self.rows()[0]
        do.resolve_authorization(self.directory, approved["id"], "approve")
        with self.assertRaisesRegex(CaptainError, r"\[authorization-required\]"):
            do.authorize(do.parse(["do", "run", "test"]), self.directory)  # another command

    def test_a_stale_approval_expires_and_a_decision_is_final(self):
        row = do.request_authorization(self.directory, do.parse(["do", "fetch"]), "captain")
        do.resolve_authorization(self.directory, row["id"], "approve")
        with patch.object(do.time, "time", return_value=time.time() + do.APPROVAL_TTL + 1):
            with self.assertRaisesRegex(CaptainError, r"\[authorization-required\]"):
                do.authorize(do.parse(["do", "fetch"]), self.directory)
        self.assertEqual([r["state"] for r in self.rows()], ["expired", "open"])
        for request, decision, code in (
            (row["id"], "approve", "authorization-closed"),
            ("nope", "deny", "authorization-unknown"),
            (self.rows()[1]["id"], "maybe", "authorization-decision"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(CaptainError, rf"\[{code}\]"):
                do.resolve_authorization(self.directory, request, decision)

    def test_a_named_request_must_be_the_approved_one_for_this_command(self):
        row = do.request_authorization(self.directory, do.parse(["do", "pull"]), "app")
        os.environ[do.AUTHORIZATION_ENV] = row["id"]
        with self.assertRaisesRegex(CaptainError, r"not an approved request"):
            do.authorize(do.parse(["do", "branch", "x"]), self.directory)

    def test_a_herdr_session_never_asks_and_writes_nothing(self):
        herdr = sessions.session(self.project, self.pane, create=True)
        with patch.object(do, "_dispatch") as dispatch, patch.object(do, "read_json") as read:
            do.run(do.parse(["do", "push"]), herdr.directory, self.project, herdr.meta)
        read.assert_not_called()
        dispatch.assert_called_once()
        self.assertFalse((herdr.directory / "protocol.json").exists())

    def test_children_never_inherit_authorization_secrets(self):
        os.environ.update({key: "secret" for key in processes.CHILD_SECRETS})
        command = [sys.executable, "-c", "import os; print(dict(os.environ))"]
        _, output = do._spawn(self.project, command)
        for key in processes.CHILD_SECRETS:
            self.assertNotIn(key, output)
        store.write_json(self.directory / "captain.json", {"provider": "codex"})
        output = io.StringIO()
        with (
            patch.object(do, "headless_argv", return_value=command),
            patch.object(do, "executable", return_value=sys.executable),
            patch.object(do, "resolve_model", return_value=None),
            patch.object(
                do, "headless_report", side_effect=lambda provider, text, model: (text, None)
            ),
            contextlib.redirect_stdout(output),
        ):
            do._quiet(do.parse(["quiet", "--task", "read"]), self.project, self.directory)
        for key in processes.CHILD_SECRETS:
            self.assertNotIn(key, output.getvalue())

    def test_changed_recipe_cannot_spend_an_approval(self):
        recipe = self.project / "Makefile"
        recipe.write_text("deploy:\n\t@echo before\n")
        args = do.parse(["do", "run", "deploy"])
        row = do.request_authorization(self.directory, args, "captain")
        do.resolve_authorization(self.directory, row["id"], "approve")
        recipe.write_text("deploy:\n\t@echo after\n")
        self.assertIsNone(do._spend(self.directory, args, None))

    def test_changed_head_or_remote_cannot_spend_an_approval(self):
        do._git(self.project, "init")
        args = do.parse(["do", "push"])
        for command in (
            ("symbolic-ref", "HEAD", "refs/heads/other"),
            ("config", "remote.origin.url", "https://example.invalid/new"),
        ):
            with self.subTest(command=command):
                row = do.request_authorization(self.directory, args, "captain")
                do.resolve_authorization(self.directory, row["id"], "approve")
                do._git(self.project, *command)
                self.assertIsNone(do._spend(self.directory, args, None))

    def test_explicit_session_cannot_escape_supervision(self):
        herdr = sessions.session(self.project, self.pane, create=True)
        os.environ["CAPTAIN_SESSION"] = self.directory.name
        args = do.parse(["do", "push"])
        args.session = herdr.directory.name
        with patch.object(do, "_dispatch") as dispatch:
            with self.assertRaisesRegex(CaptainError, "authorization-session"):
                do.run(args, herdr.directory, self.project, herdr.meta)
        dispatch.assert_not_called()

    def test_write_quiet_requires_approval_but_read_only_does_not(self):
        self.assertEqual(do.category(do.parse(["quiet", "--task", "read"])), "local")
        with self.assertRaisesRegex(CaptainError, "destructive"):
            do.authorize(do.parse(["quiet", "--task", "edit", "--write", "x"]), self.directory)

    def test_app_and_captain_approvals_cannot_be_stolen_by_other_origins(self):
        args = do.parse(["do", "push"])
        app = do.request_authorization(self.directory, args, "app")
        captain = do.request_authorization(self.directory, args, "captain")
        self.assertNotEqual(app["id"], captain["id"])
        do.resolve_authorization(self.directory, app["id"], "approve")
        self.assertIsNone(do._spend(self.directory, args, None))
        self.assertIsNotNone(do._spend(self.directory, args, app["id"]))
        do.resolve_authorization(self.directory, captain["id"], "approve")
        os.environ["CAPTAIN_ROLE"] = "crew"
        self.assertIsNone(do._spend(self.directory, args, None))

    def test_open_requests_expire_and_denials_have_a_cooldown(self):
        args = do.parse(["do", "fetch"])
        row = do.request_authorization(self.directory, args, "captain")
        with patch.object(do.time, "time", return_value=time.time() + do.APPROVAL_TTL + 1):
            with self.assertRaisesRegex(CaptainError, "authorization-closed"):
                do.resolve_authorization(self.directory, row["id"], "approve")
        row = do.request_authorization(self.directory, args, "captain")
        do.resolve_authorization(self.directory, row["id"], "deny")
        self.assertEqual(do.request_authorization(self.directory, args, "captain")["id"], row["id"])

    def test_authorization_queue_is_bounded(self):
        with patch.object(do, "AUTHORIZATION_LIMIT", 1):
            do.request_authorization(self.directory, do.parse(["do", "fetch"]), "app")
            with self.assertRaisesRegex(CaptainError, "authorization-limit"):
                do.request_authorization(self.directory, do.parse(["do", "pull"]), "app")

    def test_help_is_a_typed_refusal_without_stdout_or_system_exit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(CaptainError, "authorization-argv"):
                do.parse(["do", "push", "-h"])
        self.assertEqual(output.getvalue(), "")

    def test_requests_and_decisions_are_protocol_actions(self):
        row = do.request_authorization(
            self.directory, do.parse(["do", "push"]), "app", ["do", "push"]
        )
        do.resolve_authorization(self.directory, row["id"], "deny", "Not today.")
        eventlog.flush_actions(self.directory)
        actions, _ = eventlog.read_events(self.directory / eventlog.ACTIONS, 0)
        self.assertEqual(
            [(a["kind"], a["request"], a["state"]) for a in actions],
            [("authorization", row["id"], "open"), ("authorization", row["id"], "denied")],
        )
        self.assertEqual((actions[0]["argv"], actions[1]["reason"]), (["do", "push"], "Not today."))


class SupervisorAuthorizationTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(settings / "settings.toml", "[supervisor]\nrequest_timeout=5.0\n")
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
            self.ready = json.loads(lines.readline())

    def call(self, method, params):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(120)
            client.connect(self.ready["socket"])
            params = {"token": self.ready["token"], **params}
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            client.sendall(json.dumps(request).encode() + b"\n")
            return jsonrpc.LineReader(client.fileno()).read(time.monotonic() + 120)

    def test_the_app_runs_do_only_after_approving_its_request(self):
        store.write_text(self.project / "Makefile", "hello:\n\t@echo hi\n")
        opened = self.call("command", {"argv": ["do", "run", "hello"]})["result"]
        row = opened["authorization"]
        self.assertEqual(
            (row["state"], row["origin"], row["category"]), ("open", "app", "destructive")
        )
        again = self.call("command", {"argv": ["do", "run", "hello"]})["result"]
        self.assertEqual(again["authorization"]["id"], row["id"])
        listed = self.call("authorizations", {})["result"]["authorizations"]
        self.assertEqual([r["id"] for r in listed], [row["id"]])

        approved = self.call("resolve_authorization", {"request": row["id"], "decision": "approve"})
        result = approved["result"]
        self.assertEqual(result["state"], "spent")
        self.assertEqual(result["run"]["exit"], 0, result["run"]["stderr"])
        self.assertIn("make hello passed.", result["run"]["stdout"])
        error = self.call("resolve_authorization", {"request": row["id"], "decision": "deny"})
        self.assertEqual(error["error"]["data"], {"code": "authorization-closed"})

    def test_a_refused_verb_comes_back_typed(self):
        reply = self.call("command", {"argv": ["tell", "nobody", "hi"]})["result"]
        self.assertEqual(reply["refusal"]["code"], "captain-error")
        self.assertIn("No crew named 'nobody'", reply["refusal"]["message"])
        status = self.call("command", {"argv": ["recover"]})["result"]
        self.assertEqual(status["exit"], 0, status["stderr"])
        self.assertIsInstance(status["result"], dict)


if __name__ == "__main__":
    unittest.main()
