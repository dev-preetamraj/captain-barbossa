"""Deterministic work the captain does itself: enumerated, guarded, and recorded."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from captain_barbossa import cli, do, models
from captain_barbossa.runtime import CaptainError
from tests import home_isolation  # noqa: F401

SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "the answer",
    "num_turns": 2,
    "total_cost_usd": 0.0123,
}
# Flags that would make any verb able to rewrite, discard or publish past the fixed set.
FORBIDDEN = ("--force", "-f", "--hard", "--amend", "reset", "rebase", "--no-ff", "--rebase")


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
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            result = do.run(args, self.session, self.project)
        self.printed = buffer.getvalue()
        return result

    def advance_remote(self):
        """A remote whose main is one commit ahead, so fetch and pull have real work."""
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "remote.git")
        git(self.project, "remote", "add", "origin", str(remote))
        git(self.project, "push", "--set-upstream", "origin", "main")
        other = self.root / "other"
        subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
        git(other, "config", "user.email", "other@example.invalid")
        git(other, "config", "user.name", "Other")
        (other / "NEW.md").write_text("elsewhere\n", encoding="utf-8")
        git(other, "add", "NEW.md")
        git(other, "commit", "-m", "from elsewhere")
        git(other, "push")
        return remote

    def fake_claude(self, payload, status=0):
        """A stand-in CLI that records its argv and prints one result document."""
        argv = self.root / "argv.txt"
        binary = self.root / "bin" / "claude"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > {argv}\n'
            f"cat <<'RESULT'\n{json.dumps(payload)}\nRESULT\n"
            f"exit {status}\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        self.enterContext(
            mock.patch.dict(os.environ, {"PATH": f"{binary.parent}:{os.environ['PATH']}"})
        )
        (self.session / "captain.json").write_text(
            json.dumps({"provider": "claude"}), encoding="utf-8"
        )
        return argv

    def run_quiet(self, **values):
        return self.run_do(
            "quiet",
            **{
                "task": "what does do.py do?",
                "write": [],
                "model": "cheap",
                "timeout": None,
                "diff": None,
                **values,
            },
        )

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

    def test_branch_and_switch_refuse_a_name_that_is_not_one(self):
        for action in ("branch", "switch"):
            for name in ("-rf", "a..b", "x.lock", "trailing/", "--force"):
                with (
                    self.subTest(action=action, name=name),
                    self.assertRaisesRegex(CaptainError, "usable branch"),
                ):
                    self.run_do(action, name=name)

    def test_switch_moves_to_a_branch_that_already_exists(self):
        self.run_do("branch", name="side")
        self.run_do("switch", name="main")
        self.assertEqual(self.recorded()[-1]["action"], "switch")
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.project,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            "main",
        )

    def test_fetch_moves_the_remote_ref_and_no_worktree_file(self):
        self.advance_remote()
        self.run_do("fetch")
        self.assertFalse((self.project / "NEW.md").exists())
        self.assertEqual(self.recorded()[-1]["command"], ["git", "fetch"])
        self.assertEqual(self.head(), "initial")

    def test_pull_fast_forwards(self):
        self.advance_remote()
        self.run_do("pull")
        self.assertTrue((self.project / "NEW.md").exists())
        self.assertEqual(self.recorded()[-1]["command"], ["git", "pull", "--ff-only"])

    def test_pull_refuses_a_history_it_would_have_to_merge(self):
        self.advance_remote()
        (self.project / "README.md").write_text("diverged\n", encoding="utf-8")
        git(self.project, "commit", "--all", "-m", "diverged here")
        with self.assertRaisesRegex(CaptainError, "failed"):
            self.run_do("pull")
        self.assertFalse((self.project / "NEW.md").exists())

    def test_no_verb_can_reach_a_rewriting_or_discarding_flag(self):
        self.advance_remote()
        (self.project / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        (self.project / "README.md").write_text("two\n", encoding="utf-8")
        # Ordered so every verb can succeed: a verb that refuses because the remote is
        # ahead, or because a history diverged, has its own test.
        self.run_do("fetch")
        self.run_do("pull")
        self.run_do("commit", message="update the readme", path=["README.md"])
        self.run_do("push")
        self.run_do("branch", name="side")
        self.run_do("switch", name="main")
        self.run_do("run", target="test")
        actions = [entry["action"] for entry in self.recorded()]
        self.assertEqual(actions, ["fetch", "pull", "commit", "push", "branch", "switch", "run"])
        for entry in self.recorded():
            for forbidden in FORBIDDEN:
                with self.subTest(action=entry["action"], forbidden=forbidden):
                    self.assertNotIn(forbidden, entry["command"])

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

    def test_a_failing_target_is_loud_and_recorded_before_it_is_raised(self):
        (self.project / "Makefile").write_text(
            "test:\n\t@echo boom >&2; exit 3\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(CaptainError, "failed"):
            self.run_do("run", target="test")
        # The failed run is the one most worth reviewing, and it has no pane to review.
        # make's own status, not the recipe's: 2 is what the captain would have seen.
        (entry,) = self.recorded()
        self.assertEqual((entry["action"], entry["exit"]), ("run", 2))
        self.assertIn("boom", entry["output"])

    def test_help_is_runnable_because_it_only_describes_the_project(self):
        self.assertIn("help", do.RUNNABLE)
        (self.project / "Makefile").write_text("help:\n\t@echo targets go here\n", encoding="utf-8")
        self.run_do("run", target="help")
        self.assertIn("targets go here", self.recorded()[-1]["output"])

    def test_the_quiet_set_excludes_every_publishing_and_rewriting_verb(self):
        for forbidden in ("release", "publish", "deploy", "bump", "version", "push"):
            self.assertNotIn(forbidden, do.RUNNABLE)

    def test_quiet_reports_the_turns_own_text_and_records_the_run(self):
        argv = self.fake_claude(SUCCESS)
        self.run_quiet()
        self.assertEqual(self.printed.strip(), "the answer")
        written = argv.read_text(encoding="utf-8").splitlines()
        self.assertIn("-p", written)
        self.assertIn("what does do.py do?", written)
        self.assertIn("--output-format", written)
        self.assertIn("dontAsk", written)
        (entry,) = self.recorded()
        self.assertEqual((entry["type"], entry["action"]), ("quiet", "quiet"))
        self.assertEqual(entry["output"], "the answer")

    def test_the_quiet_prompt_forbids_nothing_the_turn_reads_with(self):
        """Codex reads through the shell alone, and a live turn refused the task over it."""
        self.assertNotIn("run commands", do.QUIET_PROMPT)
        argv = self.fake_claude(SUCCESS)
        self.run_quiet()
        written = argv.read_text(encoding="utf-8").splitlines()
        # The prompt is one line and leads the task, so it is the recorded line verbatim.
        self.assertIn(do.QUIET_PROMPT, written)

    def allowed(self, argv):
        """The tool names claude is actually allowed, not merely mentioned somewhere."""
        written = argv.read_text(encoding="utf-8").splitlines()
        start = written.index("--allowedTools") + 1
        rest = written[start:]
        end = next((i for i, word in enumerate(rest) if word.startswith("--")), len(rest))
        return rest[:end]

    def test_quiet_is_read_only_until_write_names_a_file(self):
        argv = self.fake_claude(SUCCESS)
        self.run_quiet()
        self.assertEqual(self.allowed(argv), ["Read", "Grep", "Glob"])
        written = argv.read_text(encoding="utf-8").splitlines()
        # Named on the denylist, which is where Write and Edit are allowed to appear.
        self.assertEqual(
            written[written.index("--disallowedTools") + 1 :][:3], ["Bash", "Write", "Edit"]
        )
        self.run_quiet(write=["README.md"])
        self.assertEqual(self.allowed(argv), ["Read", "Grep", "Glob", "Edit", "Write"])
        self.assertNotIn("--disallowedTools", argv.read_text(encoding="utf-8").splitlines())

    def test_quiet_refuses_a_write_path_outside_the_project_or_inside_dot_git(self):
        self.fake_claude(SUCCESS)
        for path in ("../elsewhere", ".git/config"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(CaptainError, "inside the project"),
            ):
                self.run_quiet(write=[path])
        self.assertEqual(self.recorded(), [])

    def test_quiet_refuses_a_turn_that_did_not_complete(self):
        self.fake_claude({**SUCCESS, "is_error": True, "subtype": "error_during_execution"})
        with self.assertRaisesRegex(CaptainError, "did not complete"):
            self.run_quiet()
        self.assertEqual(self.recorded(), [])

    def test_quiet_refuses_output_that_is_not_a_result_document(self):
        self.fake_claude("not a result", status=2)
        with self.assertRaisesRegex(CaptainError, "did not complete"):
            self.run_quiet()

    def test_a_failed_turn_names_the_model_it_tried_and_the_providers_own_message(self):
        self.fake_claude({**SUCCESS, "is_error": True, "subtype": "error_during_execution"})
        model = models.resolve_model("claude", "cheap")
        with self.assertRaisesRegex(
            CaptainError, f"The claude turn on {model} did not complete: error_during_execution"
        ):
            self.run_quiet()

    def test_quiet_needs_a_captain_whose_cli_has_a_headless_turn(self):
        self.fake_claude(SUCCESS)
        # Every shipped provider has one, so only an unknown CLI can reach the refusal.
        (self.session / "captain.json").write_text(
            json.dumps({"provider": "someday"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(CaptainError, "no headless turn to spend"):
            self.run_quiet()
        (self.session / "captain.json").unlink()
        with self.assertRaisesRegex(CaptainError, "start captain first"):
            self.run_quiet()

    def test_quiet_is_handed_the_diff_it_cannot_read_for_itself(self):
        argv = self.fake_claude(SUCCESS)
        (self.project / "README.md").write_text("two\n", encoding="utf-8")
        self.run_quiet(diff="worktree")
        self.assertIn("-one", argv.read_text(encoding="utf-8"))
        git(self.project, "add", "README.md")
        self.run_quiet(diff="staged")
        written = argv.read_text(encoding="utf-8")
        self.assertIn("The staged diff:", written)
        self.assertIn("+two", written)

    def test_a_long_diff_is_truncated_rather_than_sent_whole(self):
        argv = self.fake_claude(SUCCESS)
        (self.project / "README.md").write_text("x\n" * 6000, encoding="utf-8")
        self.run_quiet(diff="worktree")
        written = argv.read_text(encoding="utf-8")
        self.assertIn("(diff truncated)", written)
        self.assertLess(len(written), do.DIFF_LIMIT + 2000)

    def test_only_a_read_only_turn_is_given_this_session_to_read(self):
        argv = self.fake_claude(SUCCESS)
        self.run_quiet()
        written = argv.read_text(encoding="utf-8").splitlines()
        self.assertIn("--add-dir", written)
        self.assertIn(str(self.session), written)
        # A turn that can edit never gets the session directory, so it cannot edit mail,
        # assignment records or the event log.
        self.run_quiet(write=["README.md"])
        written = argv.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--add-dir", written)
        self.assertNotIn(str(self.session), written)

    def test_crew_may_not_run_quiet(self):
        self.enterContext(mock.patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}))
        with self.assertRaisesRegex(CaptainError, "captain-only"):
            cli.guard_crew(SimpleNamespace(command="quiet"))


class HeadlessProviderTests(unittest.TestCase):
    """Every provider's argv and result reader, against output captured from the real CLIs.

    Versions: claude 2.1.289, codex-cli 0.158.0, grok 1.0.46, pi 0.87.1. pi is the one
    success document no provider on the build machine could produce, so its shape comes
    from pi's own stream with stopReason flipped to the value it uses when a turn ends.
    """

    CLAUDE = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "pomegranate",
            "num_turns": 4,
            "total_cost_usd": 0.3558675,
        }
    )
    GROK = json.dumps(
        {
            "text": "pomegranate",
            "stopReason": "end_turn",
            "num_turns": 2,
            "total_cost_usd": 0.01652944,
        }
    )
    GROK_FAILED = json.dumps(
        {"type": "error", "message": "Couldn't set model 'no-such-model-xyz': unknown model id"}
    )
    CODEX = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "01a1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "I will do that."},
            },
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": "pomegranate"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 33772, "output_tokens": 35}},
        )
    )
    CODEX_FAILED = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "turn.started"},
            {"type": "error", "message": "model is not supported"},
            {"type": "turn.failed", "error": {"message": "model is not supported"}},
        )
    )
    PI_FAILED = json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "diagnostics": [
                    {"type": "provider_transport_failure", "error": {"message": "WebSocket error"}}
                ],
            },
        }
    )
    # Captured from a live pi turn: two provider retries that failed, the read tool call,
    # then the answer. pi relays its provider's own word, and xai finished with "stop".
    PI = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "turn_end", "message": {"content": [], "stopReason": "error"}},
            {
                "type": "turn_end",
                "message": {
                    "content": [{"type": "toolCall", "id": "call-c"}],
                    "stopReason": "toolUse",
                },
            },
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "thinkingSignature": "{}"},
                        {"type": "text", "text": "pomegranate", "textSignature": "x"},
                    ],
                    "stopReason": "stop",
                    "usage": {"totalTokens": 2741, "cost": {"total": 0.0028}},
                },
            },
        )
    )
    PI_UNFINISHED = json.dumps(
        {
            "type": "turn_end",
            "message": {"content": [{"type": "toolCall"}], "stopReason": "toolUse"},
        }
    )

    # Captured from a live pi turn against ollama/llama3.2:3b with no ollama server running:
    # pi's own errorMessage, with no diagnostics to fall back on.
    PI_CONNECTION_FAILED = json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "content": [],
                "provider": "ollama",
                "model": "llama3.2:3b",
                "stopReason": "error",
                "errorMessage": "Connection error.",
            },
        }
    )

    def test_a_failed_pi_turn_names_the_model_and_pis_own_error(self):
        with self.assertRaisesRegex(
            CaptainError,
            "The pi turn on ollama/llama3.2:3b did not complete: Connection error.",
        ):
            models.headless_report("pi", self.PI_CONNECTION_FAILED, "ollama/llama3.2:3b")

    def test_every_provider_reads_its_own_success_document(self):
        for provider, stdout in (
            ("claude", self.CLAUDE),
            ("codex", self.CODEX),
            ("grok", self.GROK),
            ("pi", self.PI),
        ):
            with self.subTest(provider=provider):
                report, note = models.headless_report(provider, stdout)
                # Codex narrates before it answers; the answer is the last message.
                self.assertEqual(report, "pomegranate")
                self.assertTrue(note.startswith("(") and note.endswith(")"), note)

    def test_an_incomplete_turn_raises_instead_of_reading_as_an_answer(self):
        for provider, stdout in (
            ("claude", json.dumps({"type": "result", "subtype": "error_during_execution"})),
            ("claude", "not a document"),
            ("codex", self.CODEX_FAILED),
            ("codex", ""),
            ("grok", self.GROK_FAILED),
            ("pi", self.PI_FAILED),
            # A turn that stopped to call a tool and never came back is not an answer.
            ("pi", self.PI_UNFINISHED),
            ("pi", ""),
        ):
            with (
                self.subTest(provider=provider, stdout=stdout[:40]),
                self.assertRaisesRegex(CaptainError, "did not complete"),
            ):
                models.headless_report(provider, stdout)

    def test_a_turn_that_cannot_write_is_never_handed_a_way_to(self):
        # The write gates, each verified against the live CLI by asking it to create a file:
        # claude BLOCKED, codex BLOCKED (only with --ignore-user-config), grok BLOCKED.
        gates = {
            "claude": ("--disallowedTools", "Bash", "Write", "Edit"),
            "codex": ("--sandbox", "read-only"),
            "grok": ("--deny", "Write", "--deny", "Edit"),
            "pi": ("--tools", "read"),
        }
        for provider, expected in gates.items():
            with self.subTest(provider=provider):
                argv = models.headless_argv(provider, provider, "task", writable=False)
                for word in expected:
                    self.assertIn(word, argv)
                written = models.headless_argv(provider, provider, "task", writable=True)
                self.assertNotEqual(argv, written)
        # A trusted project or approvals_reviewer in the user's own config.toml silently
        # defeats --sandbox, which a live run proved by writing the file anyway.
        self.assertIn("--ignore-user-config", models.headless_argv("codex", "codex", "task"))

    def test_the_task_is_the_only_thing_a_caller_puts_in_the_argv(self):
        for provider in models.PROVIDERS:
            with self.subTest(provider=provider):
                argv = models.headless_argv(provider, "/bin/agent", "--not-a-flag", writable=True)
                self.assertEqual(argv[0], "/bin/agent")
                # The task travels as one element, never split into options.
                self.assertEqual(argv.count("--not-a-flag"), 1)
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("--always-approve", argv)

    def test_only_claude_is_told_about_the_session_directory(self):
        # The others read the filesystem already; for codex --add-dir grants writes, so it
        # must never appear.
        for provider in models.PROVIDERS:
            with self.subTest(provider=provider):
                argv = models.headless_argv(provider, provider, "task", add_dir="/tmp/session")
                self.assertEqual("/tmp/session" in argv, provider == "claude")
        self.assertNotIn(
            "--add-dir", models.headless_argv("claude", "claude", "t", writable=True, add_dir="/x")
        )


if __name__ == "__main__":
    unittest.main()
