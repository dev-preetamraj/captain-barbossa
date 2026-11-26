"""`captain archive|unarchive|delete` hide, restore and remove a stopped session's state."""

import contextlib
import io
import json
import os
import subprocess
import unittest
import uuid
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    cli,
    eventlog,
    results,
    retention,
    sessions,
    store,
    supervision,
    supervisor,
    worktrees,
)


class SessionArchiveDeleteTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        self.agents = {"agents": []}
        self.enterContext(patch.object(retention, "herdr", side_effect=lambda *a, **k: self.agents))
        self.app = supervisor.open_session(self.project, None)
        self.herdr = sessions.session(self.project, self.pane, create=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def listed(self, *argv):
        code, out, _ = self.run_cli("sessions", *argv)
        self.assertEqual(code, 0)
        return {
            row["id"]: row for project in json.loads(out)["projects"] for row in project["sessions"]
        }

    def actions(self, current):
        path = current.directory / eventlog.ACTIONS
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_archive_hides_the_session_until_all_and_unarchive_restores_it(self):
        session_id = self.herdr.meta["id"]
        code, out, _ = self.run_cli("archive", "--session", session_id)
        self.assertEqual(code, 0)
        archived = json.loads(out)
        self.assertEqual((archived["session_id"], archived["archived"]), (session_id, True))
        self.assertIsInstance(archived["archived_at"], float)
        self.assertNotIn(session_id, self.listed())
        self.assertTrue(self.listed("--all")[session_id]["archived"])
        code, out, _ = self.run_cli("unarchive", "--session", session_id)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out), {"session_id": session_id, "archived": False, "archived_at": None}
        )
        self.assertNotIn("archived_at", store.read_json(self.herdr.meta_path))
        self.assertFalse(self.listed()[session_id]["archived"])

    def test_archiving_twice_keeps_the_first_time_and_logs_once(self):
        first = retention.archive_session(self.app.directory)
        self.assertEqual(retention.archive_session(self.app.directory), first)
        self.assertEqual([row["kind"] for row in self.actions(self.app)], ["session-archived"])
        sessions.set_archived(self.app.directory, False)
        self.assertEqual(
            [(row["kind"], row["session_id"]) for row in self.actions(self.app)],
            [
                ("session-archived", self.app.meta["id"]),
                ("session-unarchived", self.app.meta["id"]),
            ],
        )

    def test_a_herdr_session_logs_no_archive_action(self):
        retention.archive_session(self.herdr.directory)
        self.assertEqual(self.actions(self.herdr), [])

    def test_plain_sessions_output_is_the_catalog_and_json_all_asks_for_archived(self):
        retention.archive_session(self.herdr.directory)
        code, out, _ = self.run_cli("sessions")
        self.assertEqual(json.loads(out), sessions.catalog(self.project))
        with patch.object(supervisor, "counted_catalog", return_value={}) as counted:
            self.run_cli("sessions", "--json", "--all")
        counted.assert_called_once_with(self.project, include_archived=True)

    def test_a_running_supervisor_refuses_archive_and_delete_with_a_typed_code(self):
        with store.lock(self.app.directory / supervision.LOCK):
            for verb in ("archive", "delete"):
                code, out, err = self.run_cli(verb, "--session", self.app.meta["id"])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("[supervisor-running]", err)
        self.assertNotIn("archived_at", store.read_json(self.app.meta_path))
        self.assertTrue(self.app.directory.is_dir())

    def test_live_herdr_agents_refuse_archive_and_delete(self):
        crew = f"c-{self.herdr.meta['id'][:8]}-jack"
        self.agents = {"agents": [{"terminal_id": "t1", "name": crew}]}
        for verb in ("archive", "delete"):
            code, _, err = self.run_cli(verb, "--session", self.herdr.meta["id"])
            self.assertEqual(code, 1)
            self.assertIn("[session-live-agents]", err)
        self.assertTrue(self.herdr.directory.is_dir())

    def test_delete_removes_only_that_session_directory(self):
        code, out, _ = self.run_cli("delete", "--session", self.app.meta["id"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"session_id": self.app.meta["id"], "deleted": True})
        self.assertFalse(self.app.directory.exists())
        self.assertTrue(self.herdr.directory.is_dir())
        self.assertNotIn(self.app.meta["id"], self.listed("--all"))

    def test_delete_refuses_a_path_that_is_not_a_session_directory(self):
        elsewhere = self.root / "keep" / uuid.uuid4().hex
        elsewhere.mkdir(parents=True)
        store.write_json(elsewhere / "session.json", {"id": elsewhere.name})
        link = self.app.directory.parent / uuid.uuid4().hex
        link.symlink_to(elsewhere)
        for path in (elsewhere, link, self.app.directory.parent):
            with self.assertRaisesRegex(Exception, r"\[delete-outside-session\]"):
                retention.delete_session(path)
        self.assertTrue((elsewhere / "session.json").is_file())

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.project, check=True, capture_output=True)

    def reserve_worktree(self):
        self.git("init")
        self.git(
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-m",
            "b",
            "--allow-empty",
        )
        return worktrees.reserve(self.herdr, "Elizabeth", uuid.uuid4().hex, uuid.uuid4().hex)

    def test_a_worktree_not_removed_refuses_delete_and_is_listed(self):
        record = self.reserve_worktree()
        code, _, err = self.run_cli("delete", "--session", self.herdr.meta["id"])
        self.assertEqual(code, 1)
        self.assertIn("[session-unlanded-work]", err)
        self.assertIn(f"worktree {record['id']}", err)
        self.assertTrue(self.herdr.directory.is_dir())

    def test_an_unreviewed_result_refuses_delete_until_it_is_discarded(self):
        record = self.reserve_worktree()
        store.write_json(
            worktrees.record_path(self.herdr, record["id"]), {**record, "phase": "removed"}
        )
        manifest = {
            "id": uuid.uuid4().hex,
            "session_id": self.herdr.meta["id"],
            "project": str(self.project),
            "worktree_id": record["id"],
            "entries": [],
            **{key: record[key] for key in ("incarnation_id", "assignment_id", "crew_id")},
            **{key: record[key] for key in ("base_oid", "execution_root")},
        }
        manifest["manifest_hash"] = results._hash(manifest)
        folder = self.herdr.directory / "results" / manifest["id"]
        folder.mkdir(parents=True)
        store.write_json(folder / "manifest.json", manifest)
        code, _, err = self.run_cli("delete", "--session", self.herdr.meta["id"])
        self.assertEqual(code, 1)
        self.assertIn("[session-unreviewed-results]", err)
        self.assertIn(f"result {manifest['id']}", err)
        store.write_json(folder / "discard.json", {"state": "discarded"})
        code, _, _ = self.run_cli("delete", "--session", self.herdr.meta["id"])
        self.assertEqual(code, 0)
        self.assertFalse(self.herdr.directory.exists())


if __name__ == "__main__":
    unittest.main()
