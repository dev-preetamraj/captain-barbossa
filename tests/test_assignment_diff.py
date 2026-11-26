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
