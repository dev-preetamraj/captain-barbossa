"""Deterministic work the captain does itself: enumerated, guarded, and recorded."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from captain_barbossa import do
from captain_barbossa.runtime import CaptainError
from tests import home_isolation  # noqa: F401


def git(root, *arguments):
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class DoTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.session = self.root / "session"
        (self.session / "events").mkdir(parents=True)
        git(self.project, "init", "--initial-branch=main")
        git(self.project, "config", "user.email", "crew@example.invalid")
        git(self.project, "config", "user.name", "Crew")
        (self.project / "README.md").write_text("one\n", encoding="utf-8")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-m", "initial")

    def run_do(self, command, **values):
        args = SimpleNamespace(do_command=command, **values)
        with contextlib.redirect_stdout(io.StringIO()):
            return do.run(args, self.session, self.project)

    def recorded(self):
        path = self.session / "events" / "captain.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def head(self):
        return subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_commit_stages_the_named_paths_and_records_the_run(self):
        (self.project / "README.md").write_text("two\n", encoding="utf-8")
        self.run_do("commit", message="update the readme", path=["README.md"])
        self.assertEqual(self.head(), "update the readme")
        (entry,) = self.recorded()
        self.assertEqual(entry["action"], "commit")
        self.assertEqual(entry["exit"], 0)
        self.assertIn("update the readme", entry["command"])

    def test_commit_refuses_when_nothing_is_staged(self):
        with self.assertRaisesRegex(CaptainError, "Nothing staged"):
            self.run_do("commit", message="empty", path=[])
        self.assertEqual(self.recorded(), [])

    def test_commit_refuses_a_path_outside_the_project(self):
        with self.assertRaisesRegex(CaptainError, "inside the project"):
            self.run_do("commit", message="escape", path=["../elsewhere"])

    def test_commit_refuses_a_path_inside_dot_git(self):
        with self.assertRaisesRegex(CaptainError, "outside .git"):
            self.run_do("commit", message="tamper", path=[".git/config"])

    def test_branch_creates_and_switches(self):
        self.run_do("branch", name="fm/quiet-work")
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(current, "fm/quiet-work")
        self.assertEqual(self.recorded()[0]["action"], "branch")

    def test_branch_refuses_a_name_that_is_not_one(self):
        for name in ("-rf", "a..b", "x.lock", "trailing/", "--force"):
            with self.subTest(name=name), self.assertRaisesRegex(CaptainError, "usable branch"):
                self.run_do("branch", name=name)

    def test_run_refuses_a_target_outside_the_quiet_set(self):
        (self.project / "Makefile").write_text("release:\n\t@true\n", encoding="utf-8")
        with self.assertRaisesRegex(CaptainError, "not a quiet target"):
            self.run_do("run", target="release")

    def test_run_refuses_a_target_the_project_does_not_declare(self):
        (self.project / "Makefile").write_text("build:\n\t@true\n", encoding="utf-8")
        with self.assertRaisesRegex(CaptainError, "declares no test target"):
            self.run_do("run", target="test")

    def test_run_refuses_when_there_is_no_makefile(self):
        with self.assertRaisesRegex(CaptainError, "No Makefile"):
            self.run_do("run", target="test")

    def test_run_executes_a_declared_target_and_records_it(self):
        (self.project / "Makefile").write_text("test:\n\t@echo ran the suite\n", encoding="utf-8")
        self.run_do("run", target="test")
        (entry,) = self.recorded()
        self.assertEqual((entry["action"], entry["exit"]), ("run", 0))
        self.assertIn("ran the suite", entry["output"])

    def test_a_failing_target_is_loud_and_not_swallowed(self):
        (self.project / "Makefile").write_text(
            "test:\n\t@echo boom >&2; exit 3\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(CaptainError, "failed"):
            self.run_do("run", target="test")

    def test_the_quiet_set_excludes_every_publishing_and_rewriting_verb(self):
        for forbidden in ("release", "publish", "deploy", "bump", "version", "push"):
            self.assertNotIn(forbidden, do.RUNNABLE)


if __name__ == "__main__":
    unittest.main()
