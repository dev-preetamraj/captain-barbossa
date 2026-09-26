"""Bounded reads exercise real temp files and Git without launching native agents."""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import inspection, memory
from captain_barbossa.runtime import CaptainError


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_ROLE": "captain",
                    "CAPTAIN_TEMP_ROOT": str(self.root / "temp"),
                    "CAPTAIN_STATE_ROOT": str(self.root / "state"),
                },
            )
        )
        self.enterContext(patch.object(memory, "_warned_temp_state_root", True))
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument("--session")
        inspection.add_arguments(self.parser.add_subparsers(dest="command", required=True))

    def inspect(self, *args, session=None):
        argv = (["--session", session] if session else []) + ["inspect", *args]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            inspection.run(self.parser.parse_args(argv), self.project)
        self.assertLessEqual(len(out.getvalue().encode()), inspection.MAX_BYTES)
        return json.loads(out.getvalue())

    def test_sorted_files_literal_search_and_escaped_output(self):
        for name in ("z", "a", "$(touch nope)", "--help", "[literal]"):
            (self.project / name).write_text("plain\na.*b\n\x1b[31m", encoding="utf-8")
        paths = self.inspect("files")["paths"]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(
            self.inspect("search", "a.*b", "[literal]")["matches"],
            [{"path": "[literal]", "line": 2, "text": "a.*b"}],
        )
        self.assertIn("plain", self.inspect("read", "--", "--help")["text"])
        self.assertFalse((self.project / "nope").exists())

    def test_outside_paths_traversal_and_symlinks_are_rejected(self):
        outside = self.root / "secret"
        outside.write_text("secret", encoding="utf-8")
        (self.project / "link").symlink_to(outside)
        (self.project / "dir").symlink_to(self.root, target_is_directory=True)
        for path in (str(outside), "../secret", "link", "dir/secret"):
            with self.subTest(path=path), self.assertRaises(CaptainError):
                self.inspect("read", path)
        self.assertEqual(self.inspect("files")["paths"], [])

    def test_fifo_and_binary_are_rejected_without_blocking(self):
        os.mkfifo(self.project / "pipe")
        (self.project / "binary").write_bytes(b"\0\xff")
        for name in ("pipe", "binary"):
            with self.subTest(name=name), self.assertRaises(CaptainError):
                self.inspect("read", name)

    def test_results_and_traversal_are_bounded(self):
        for name in ("a", "b", "c"):
            (self.project / name).touch()
        with patch.object(inspection, "MAX_RESULTS", 2):
            self.assertEqual(self.inspect("files"), {"paths": ["a", "b"], "truncated": True})
        with (
            patch.object(inspection, "MAX_ENTRIES", 2),
            self.assertRaisesRegex(CaptainError, "limit"),
        ):
            self.inspect("files")

    def test_output_size_and_deadline_have_stable_failures(self):
        (self.project / "big").write_text("x" * (inspection.MAX_BYTES + 1))
        with self.assertRaisesRegex(CaptainError, "Inspection limit"):
            self.inspect("read", "big")
        with (
            patch.object(inspection, "TIMEOUT", 0),
            self.assertRaisesRegex(CaptainError, "timeout"),
        ):
            self.inspect("files")

    def test_state_is_scoped_and_never_initializes_missing_storage(self):
        with self.assertRaisesRegex(CaptainError, "missing"):
            self.inspect("state", "project")
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "temp").exists())
        base = memory.read_storage(memory.state_root(), self.project)
        base.mkdir(parents=True)
        (base / "graph.json").write_text("{}")
        self.assertEqual(self.inspect("state", "project", "graph.json")["text"], "{}")
        for path in (str(self.root), "../../", str(self.project)):
            with self.subTest(path=path), self.assertRaises(CaptainError):
                self.inspect("state", "project", path)

    def test_state_session_requires_identity_and_bounded_matching_metadata(self):
        with self.assertRaisesRegex(CaptainError, "session"):
            self.inspect("state", "session")
        session = "a" * 32
        base = memory.read_storage(memory.temp_root(), self.project) / "sessions" / session
        base.mkdir(parents=True)
        meta = base / "session.json"
        meta.write_text(json.dumps({"project": str(self.project)}))
        self.assertEqual(
            self.inspect("state", "session", session=session)["paths"], ["session.json"]
        )
        meta.write_text(json.dumps({"project": str(self.root)}))
        with self.assertRaisesRegex(CaptainError, "another project"):
            self.inspect("state", "session", session=session)
        meta.write_text(" " * (inspection.MAX_BYTES + 1))
        with self.assertRaisesRegex(CaptainError, "exceeds"):
            self.inspect("state", "session", session=session)

    def test_state_scope_symlinks_are_rejected(self):
        (self.project / ".captain").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(CaptainError, "symlink"):
            self.inspect("state", "repo")

    def test_crew_state_scope_is_enforced_before_any_private_state_lookup(self):
        (self.project / ".captain").mkdir()
        (self.project / ".captain/graph.json").write_text("{}")
        (self.project / "source").write_text("source")
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}):
            for scope in ("session", "project"):
                with (
                    self.subTest(scope=scope),
                    patch.object(inspection, "_state_root", side_effect=AssertionError("lookup")),
                    self.assertRaisesRegex(CaptainError, "forbidden-scope"),
                ):
                    self.inspect("state", scope)
            self.assertEqual(self.inspect("state", "repo", "graph.json")["text"], "{}")
            self.assertEqual(self.inspect("read", "source")["text"], "source")
            with self.assertRaisesRegex(CaptainError, "outside-root"):
                self.inspect("read", str(self.root / "secret"))

    def git(self, *args):
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        return subprocess.run(
            [str(inspection.GIT), *args],
            cwd=self.project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def init_git(self):
        self.git("init", "-q", "--template=", "-b", "main")
        (self.project / "file").write_text("original\n")
        self.git("add", "file")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        )

    def test_fixed_git_queries_work_and_do_not_change_index(self):
        self.init_git()
        index = self.project / ".git/index"
        before = index.read_bytes(), index.stat().st_mtime_ns
        (self.project / "file").write_text("changed\n")
        self.assertIn(" M file", self.inspect("git", "status")["text"])
        self.assertEqual(self.inspect("git", "current-branch")["text"], "main\n")
        self.assertEqual(self.inspect("git", "root")["text"].strip(), str(self.project))
        self.assertIn("initial", self.inspect("git", "log")["text"])
        self.assertIn("+changed", self.inspect("git", "diff")["text"])
        self.assertEqual(self.inspect("git", "diff", "--staged")["text"], "")
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        self.git("add", "file")
        self.assertIn("+changed", self.inspect("git", "diff", "--staged")["text"])
        self.assertEqual(self.inspect("git", "diff")["text"], "")

    def test_git_metadata_disappearing_before_stat_or_open_is_tolerated(self):
        self.init_git()
        lock = self.project / ".git/maintenance.lock"
        original_open, original_scandir = os.open, os.scandir

        def open_after_removal(path, flags, **kwargs):
            if path == lock.name:
                lock.unlink()
            return original_open(path, flags, **kwargs)

        @contextlib.contextmanager
        def scan_before_removal(fd):
            with original_scandir(fd) as entries:
                entries = list(entries)
                if any(entry.name == lock.name for entry in entries):
                    lock.unlink()
                yield iter(entries)

        for operation, replacement in (
            ("open", open_after_removal),
            ("scandir", scan_before_removal),
        ):
            with self.subTest(operation=operation):
                lock.touch()
                with patch.object(inspection.os, operation, replacement):
                    self.assertEqual(self.inspect("git", "current-branch")["text"], "main\n")
                self.assertFalse(lock.exists())

    def test_metadata_race_handling_preserves_missing_permission_and_symlink_errors(self):
        self.init_git()
        for metadata in (False, True):
            with self.subTest(metadata=metadata), self.assertRaises(FileNotFoundError):
                list(
                    inspection._walk(self.project, Path("missing"), float("inf"), metadata=metadata)
                )
        lock = self.project / ".git/maintenance.lock"
        lock.touch()
        original_open = os.open

        def denied(path, flags, **kwargs):
            if path == lock.name:
                raise PermissionError("denied")
            return original_open(path, flags, **kwargs)

        with (
            patch.object(inspection.os, "open", denied),
            self.assertRaisesRegex(CaptainError, "unreadable"),
        ):
            self.inspect("git", "status")

        def swapped(path, flags, **kwargs):
            if path == lock.name:
                lock.unlink()
                lock.symlink_to(self.root)
            return original_open(path, flags, **kwargs)

        with (
            patch.object(inspection.os, "open", swapped),
            self.assertRaisesRegex(CaptainError, "unreadable"),
        ):
            self.inspect("git", "status")
        lock.unlink()
        (self.project / ".git/config").unlink()
        with self.assertRaisesRegex(CaptainError, "missing"):
            self.inspect("git", "status")

    def test_git_scrubs_environment_and_ignores_user_configuration(self):
        self.init_git()
        poison = self.root / "poison"
        poison.write_text("[include]\npath = /does/not/exist\n[core]\nfsmonitor = false\n")
        with patch.dict(
            os.environ,
            {
                "PATH": str(self.root),
                "GIT_DIR": "/missing",
                "GIT_WORK_TREE": "/missing",
                "GIT_CONFIG_GLOBAL": str(poison),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": "touch unwanted",
                "GIT_EXTERNAL_DIFF": "touch unwanted",
                "LD_PRELOAD": "unwanted",
            },
        ):
            self.assertEqual(self.inspect("git", "current-branch")["text"], "main\n")
        self.assertFalse((self.project / "unwanted").exists())

    def test_unsafe_git_configs_are_rejected_before_launch(self):
        self.init_git()
        config = self.project / ".git/config"
        original = config.read_text()
        for extra in (
            "[include]\npath = /tmp/other\n",
            "[core]\nfsmonitor = command\n",
            '[filter "evil"]\nclean = command\n',
            "[diff]\nexternal = command\n",
            "[core]\nworktree = /tmp\n",
            "[extensions]\npartialClone = origin\n",
        ):
            config.write_text(original + extra)
            with self.subTest(extra=extra), patch.object(inspection.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(CaptainError, "unsupported-git"):
                    self.inspect("git", "status")
                popen.assert_not_called()

    def test_git_metadata_symlinks_and_alternates_are_rejected(self):
        self.init_git()
        alternate = self.project / ".git/objects/info/alternates"
        alternate.parent.mkdir(exist_ok=True)
        alternate.write_text(str(self.root))
        with self.assertRaisesRegex(CaptainError, "unsupported-git"):
            self.inspect("git", "status")
        alternate.unlink()
        (self.project / ".git/objects/escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(CaptainError, "unsupported-git"):
            self.inspect("git", "status")

    def test_linked_worktrees_and_arbitrary_options_are_rejected(self):
        (self.project / ".git").write_text("gitdir: /outside")
        with self.assertRaisesRegex(CaptainError, "unsupported-git"):
            self.inspect("git", "status")
        with self.assertRaisesRegex(CaptainError, "invalid-option"):
            self.inspect("git", "log", "--staged")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["inspect", "git", "diff", "--ext-diff"])

    def test_git_output_and_runtime_are_bounded(self):
        self.init_git()
        popen = subprocess.Popen
        for script, timeout, error in (
            ("import time; time.sleep(10)", 0.2, "timeout"),
            ("print('x' * 100000)", 5, "limit"),
        ):

            def launch(command, **kwargs):
                return popen([sys.executable, "-c", script], **kwargs)

            with (
                self.subTest(error=error),
                patch.object(inspection, "TIMEOUT", timeout),
                patch.object(inspection.subprocess, "Popen", side_effect=launch),
                self.assertRaisesRegex(CaptainError, error),
            ):
                self.inspect("git", "status")


if __name__ == "__main__":
    unittest.main()
