"""Structured session-delete blockers and explicit discard overrides."""

import contextlib
import io
import json
import os
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from home_isolation import SessionCase

from captain_barbossa import cli, retention, store, supervisor


class SessionDeleteForceTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(retention, "herdr", return_value={"agents": []}))
        self.current = supervisor.open_session(self.project, None)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def blocked_session(self):
        assignment_id = uuid.uuid4().hex
        question_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        state = {
            "active": {"jack": assignment_id},
            "assignments": {
                assignment_id: {
                    "id": assignment_id,
                    "crew": "Jack",
                    "incarnation_id": uuid.uuid4().hex,
                    "state": "asked",
                    "question": {"id": question_id, "text": "Which option should I use?"},
                    "report": None,
                    "messages": [],
                    "notices": [],
                    "ack_seq": 0,
                    "pending": None,
                }
            },
        }
        store.write_json(self.current.directory / "protocol.json", state)
        mail = self.current.directory / "mail" / "jack"
        mail.mkdir(parents=True)
        store.write_json(
            mail / f"{message_id}.json",
            {
                "id": message_id,
                "assignment_id": assignment_id,
                "state": "bounced",
                "reason": "the recruit failed before launch",
            },
        )
        return assignment_id

    def test_cli_json_and_plain_refusals_list_structured_blockers(self):
        assignment_id = self.blocked_session()
        code, out, err = self.run_cli("delete", "--session", self.current.meta["id"], "--json")
        self.assertEqual((code, err), (1, ""))
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "session-unresolved")
        blockers = error["data"]["blockers"]
        self.assertEqual({row["kind"] for row in blockers}, {"question", "mail"})
        self.assertTrue(
            all(
                set(row) == {"kind", "crew", "assignment_id", "summary"}
                and row["crew"] == "jack"
                and row["assignment_id"] == assignment_id
                for row in blockers
            )
        )
        code, out, err = self.run_cli("delete", "--session", self.current.meta["id"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("blocker (question):", err)
        self.assertIn("blocker (mail):", err)

    def test_force_deletes_unreceived_question_and_bounced_mail(self):
        self.blocked_session()
        code, out, err = self.run_cli("delete", "--session", self.current.meta["id"], "--force")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"session_id": self.current.meta["id"], "deleted": True})
        self.assertFalse(self.current.directory.exists())

    def test_supervisor_refusal_carries_blockers(self):
        target = supervisor.open_session(self.project, None)
        self.current = target
        assignment_id = self.blocked_session()
        caller = supervisor.open_session(self.project, None)
        service = supervisor.Supervisor(caller, "token", threading.Event())
        reply = supervisor.Handler.answer(
            service,
            "delete_session",
            {"token": "token", "session_id": target.meta["id"]},
        )
        blockers = reply["error"]["data"]["blockers"]
        self.assertEqual(reply["error"]["data"]["code"], "session-unresolved")
        self.assertEqual({row["assignment_id"] for row in blockers}, {assignment_id})

    def test_force_does_not_override_a_running_supervisor(self):
        with store.lock(self.current.directory / "supervisor.lock"):
            with self.assertRaises(retention.SessionDeleteError) as raised:
                retention.delete_session(self.current.directory, force=True)
        self.assertEqual(raised.exception.blockers, [])
        self.assertIn("[supervisor-running]", str(raised.exception))
        self.assertTrue(self.current.directory.exists())

    def test_discard_worktrees_uses_result_and_retirement_cleanup(self):
        record = {"id": uuid.uuid4().hex, "phase": "ready"}
        manifest = {"id": uuid.uuid4().hex}
        with (
            patch.object(retention.worktrees, "inventory", return_value=[record]),
            patch.object(retention.worktrees, "stopped", return_value=True),
            patch.object(retention.results, "collect", return_value=manifest) as collect,
            patch.object(retention.results, "discard") as discard,
        ):
            dispose = Mock()
            retention._discard_worktrees(self.current, dispose)
        collect.assert_called_once_with(self.current, record["id"], ["."])
        discard.assert_called_once_with(
            self.current, manifest["id"], "Session deletion discarded this worktree."
        )
        dispose.assert_called_once_with(self.current, record["id"])

    def test_cli_passes_force_and_discard_worktrees_flags(self):
        deleted = {"session_id": self.current.meta["id"], "deleted": True}
        with patch.dict(cli.ARCHIVE_VERBS, {"delete": unittest.mock.Mock(return_value=deleted)}):
            command = cli.ARCHIVE_VERBS["delete"]
            code, out, err = self.run_cli(
                "delete",
                "--session",
                self.current.meta["id"],
                "--force",
                "--discard-worktrees",
            )
        self.assertEqual((code, err, json.loads(out)), (0, "", deleted))
        command.assert_called_once_with(
            self.current.directory,
            force=True,
            discard_worktrees=True,
            worktree_cleanup=cli.cleanup_worktree,
        )


if __name__ == "__main__":
    unittest.main()
