"""Per-assignment review: a supervised assignment's diff covers only its own paths since the
tree snapshot taken when it began, and marking its report reviewed is recorded in captain state."""

import contextlib
import os
import subprocess
import threading
import types
import unittest
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import eventlog, protocol, sessions, store, supervision, supervisor
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError

INCARNATION = "b" * 32


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class AssignmentDiffTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        git(self.project, "init", "-q")
        for name in ("a.txt", "other.txt"):
            (self.project / name).write_text("one\n", encoding="utf-8")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "base")
        self.current = supervisor.open_session(self.project, None)
        self.owner = supervisor.Supervisor(self.current, "token", threading.Event())

    def crew(self, current, execution="supervised"):
        record = {
            "id": "will",
            "agent": "captain-will",
            "incarnation_id": INCARNATION,
            "provider": "codex",
            "execution": execution,
        }
        with sessions.crew_meta(current.directory) as meta:
            meta["crew"]["will"] = record
        return Crew("will", record, current._replace(meta=meta))

    def test_the_diff_shows_only_this_assignments_paths_since_it_began(self):
        (self.project / "a.txt").write_text("dirty before\n", encoding="utf-8")
        index = git(self.project, "ls-files", "--stage")
        assignment = protocol.begin(
            self.crew(self.current), self.project, "Edit a.", ["a.txt", "new.txt"], ["edit"]
        )
        self.assertRegex(assignment["baseline"], r"^[0-9a-f]{40}$")
        (self.project / "a.txt").write_text("dirty before\nand after\n", encoding="utf-8")
        (self.project / "new.txt").write_text("fresh\n", encoding="utf-8")
        (self.project / "other.txt").write_text("someone else\n", encoding="utf-8")
        view = supervisor.diff(self.owner, {"assignment": assignment["id"]})
        self.assertEqual(
            view["files"],
            [
                {"path": "a.txt", "added": 1, "deleted": 0},
                {"path": "new.txt", "added": 1, "deleted": 0},
            ],
        )
        self.assertIn("+and after", view["diff"])
        self.assertNotIn("-one", view["diff"])  # the earlier dirty edit is in the baseline
        self.assertNotIn("+dirty before", view["diff"])
        self.assertNotIn("other.txt", view["diff"])
        self.assertEqual(git(self.project, "ls-files", "--stage"), index)  # real index untouched

    def loose_objects(self):
        return sorted(
            path.relative_to(self.project).as_posix()
            for path in (self.project / ".git" / "objects").rglob("*")
            if path.is_file() and path.parent.name not in ("pack", "info")
        )

    def test_a_snapshot_leaves_the_repository_untouched_and_runs_no_user_code(self):
        marker = self.root / "ran"
        git(self.project, "update-index", "--split-index")
        indexes = sorted((self.project / ".git").glob("*index*"))
        index = {path.name: path.read_bytes() for path in indexes}
        objects = self.loose_objects()
        hook = f"sh -c 'touch {marker}; cat'"
        git(self.project, "config", "filter.mark.clean", hook)
        git(self.project, "config", "core.fsmonitor", f"sh -c 'touch {marker}'")
        (self.project / ".gitattributes").write_text("*.txt filter=mark\n", encoding="utf-8")
        marker.unlink(missing_ok=True)
        assignment = protocol.begin(
            self.crew(self.current), self.project, "Edit a.", ["a.txt"], ["edit"]
        )
        (self.project / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        view = protocol.assignment_diff(self.current.directory, assignment["id"])
        self.assertEqual(view["files"], [{"path": "a.txt", "added": 1, "deleted": 0}])
        self.assertFalse(marker.exists())
        self.assertEqual(self.loose_objects(), objects)
        self.assertEqual({path.name: path.read_bytes() for path in indexes}, index)
        self.assertEqual(len(indexes), 2)  # the split index and its shared index
        self.assertTrue(any((self.current.directory / protocol.SNAPSHOTS).rglob("*")))

    def test_paths_are_literal_and_relative_to_an_execution_root_inside_the_repo(self):
        project = self.project / "app"
        project.mkdir()
        for name in ("a*.txt", "ab.txt"):
            (project / name).write_text("one\n", encoding="utf-8")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "app")
        current = supervisor.open_session(project, None)
        crew = self.crew(current)
        assignment = protocol.begin(crew, project, "Edit.", ["a*.txt"], ["edit"])
        for name in ("a*.txt", "ab.txt"):
            (project / name).write_text("two\n", encoding="utf-8")
        view = protocol.assignment_diff(current.directory, assignment["id"])
        self.assertEqual([row["path"] for row in view["files"]], ["a*.txt"])

    def test_clean_filtered_files_reuse_index_blobs_without_hashing(self):
        (self.project / ".gitattributes").write_text("*.txt text eol=crlf\n")
        (self.project / "a.txt").write_bytes(b"one\r\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "normalize")
        git(self.project, "update-index", "--refresh")
        with patch.object(protocol, "_file_rows", wraps=protocol._file_rows) as rows:
            assignment = protocol.begin(self.crew(self.current), self.project, "Read.")
        hashed = {path for call in rows.call_args_list for path in call.args[1] if path}
        self.assertEqual(hashed, set())
        self.assertEqual(
            assignment["baseline"], git(self.project, "rev-parse", "HEAD^{tree}").strip()
        )

    def test_repeated_diffs_do_not_grow_snapshot_store(self):
        assignment = protocol.begin(
            self.crew(self.current), self.project, "Edit.", ["a.txt"], ["edit"]
        )
        objects = self.current.directory / protocol.SNAPSHOTS
        before = {
            path.relative_to(objects): path.read_bytes()
            for path in objects.rglob("*")
            if path.is_file()
        }
        for value in ("two\n", "three\n"):
            (self.project / "a.txt").write_text(value)
            view = protocol.assignment_diff(self.current.directory, assignment["id"])
            self.assertIn("+" + value, view["diff"])
        after = {
            path.relative_to(objects): path.read_bytes()
            for path in objects.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_baseline_filesystem_error_is_recorded_without_aborting_assignment(self):
        with patch.object(protocol, "_file_rows", side_effect=PermissionError("unreadable")):
            assignment = protocol.begin(self.crew(self.current), self.project, "Read.")
        self.assertIsNone(assignment["baseline"])
        self.assertIn("unreadable", assignment["baseline_error"])

    def test_a_baseline_follows_validation_and_its_failure_is_reported(self):
        crew = self.crew(self.current)
        protocol.begin(crew, self.project, "Edit a.", ["a.txt"], ["edit"])
        other = self.crew(supervisor.open_session(self.project, None))
        with (
            patch.object(protocol, "snapshot", side_effect=AssertionError("snapshot")),
            self.assertRaisesRegex(CaptainError, r"\[owned-path-conflict\]"),
        ):
            protocol.begin(other, self.project, "Edit a too.", ["a.txt"], ["edit"])
        plain = self.root / "plain"
        plain.mkdir()
        current = supervisor.open_session(plain, None)
        assignment = protocol.begin(self.crew(current), plain, "Edit b.", ["b.txt"], ["edit"])
        self.assertIsNone(assignment["baseline"])
        self.assertIn("[no-git]", assignment["baseline_error"])
        with self.assertRaisesRegex(CaptainError, r"\[diff-no-baseline\].*\[no-git\]"):
            protocol.assignment_diff(current.directory, assignment["id"])

    def test_a_herdr_assignment_takes_no_snapshot(self):
        herdr = sessions.session(self.project, self.pane, create=True)
        with patch.object(protocol, "snapshot", side_effect=AssertionError("snapshot")):
            assignment = protocol.begin(self.crew(herdr, None), self.project, "Edit a.")
        self.assertNotIn("baseline", assignment)
        with self.assertRaisesRegex(CaptainError, r"\[diff-no-baseline\]"):
            protocol.assignment_diff(herdr.directory, assignment["id"])
        with self.assertRaisesRegex(CaptainError, r"\[diff-assignment\]"):
            protocol.assignment_diff(herdr.directory, "nope")

    def test_a_report_is_marked_reviewed_in_captain_state(self):
        crew = self.crew(self.current)
        assignment = protocol.begin(crew, self.project, "Map the repo.")["id"]
        with self.assertRaisesRegex(CaptainError, r"\[review-no-report\]"):
            supervisor.mark_reviewed(self.owner, {"assignment": assignment})
        protocol.change(
            crew,
            types.SimpleNamespace(
                command="done", assignment=assignment, incarnation=None, report="Mapped."
            ),
            self.project,
        )
        marked = supervisor.mark_reviewed(self.owner, {"assignment": assignment, "note": "LGTM"})
        self.assertEqual((marked["assignment"], marked["note"]), (assignment, "LGTM"))
        state = store.read_json(self.current.directory / "protocol.json")
        self.assertEqual(state["assignments"][assignment]["reviewed"]["note"], "LGTM")
        eventlog.flush_actions(self.current.directory)
        actions, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        self.assertEqual(actions[-1]["kind"], "review")
        with self.assertRaisesRegex(CaptainError, r"\[review-result\]"):
            supervisor.mark_reviewed(self.owner, {"assignment": assignment, "result": "a" * 32})


if __name__ == "__main__":
    unittest.main()
