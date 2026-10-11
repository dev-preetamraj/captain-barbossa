import contextlib
import io
import json
import shlex
import sys
import unittest
from unittest.mock import patch

from captain_barbossa import events, protocol, sessions, store
from captain_barbossa.approval import REASONS, Decision, Request, Verdict, decide
from captain_barbossa.crew import Crew, event_status
from tests.home_isolation import SessionCase

PROJECT = "/repo"
OWNED = ("src/mine.py", "tests/test_mine.py", "docs/notes")
ALL_ACTIONS = ("edit", "test", "format", "commit")


def bash(command, actions=ALL_ACTIONS, cwd=PROJECT):
    return decide(Request("Bash", PROJECT, cwd, OWNED, actions, command=command))


class ApprovalTests(unittest.TestCase):
    def check(self, cases, run):
        for value, code in cases:
            with self.subTest(value=value):
                self.assertEqual(run(value).code, code)

    def test_every_code_has_one_closed_verdict(self):
        self.assertEqual({verdict for verdict, _ in REASONS.values()}, set(Verdict))
        self.assertEqual(Decision("granted").verdict, Verdict.ALLOW_ONCE)
        self.assertEqual(Decision("destructive").verdict, Verdict.ESCALATE)
        self.assertEqual(Decision("action-not-granted").verdict, Verdict.DENY)

    def test_shell_inside_grants_is_allowed_once(self):
        self.check(
            [
                ("git status", "granted"),
                ("git diff -- src/other.py | head -50", "granted"),
                ("cat src/other.py && grep -n foo docs/x.md", "granted"),
                ("sed -n 1,40p src/other.py", "granted"),
                ("make test 2>&1 | tail -20", "granted"),
                ("uv run --locked python -m unittest tests.test_mine -v", "granted"),
                ("uv run --locked python -m unittest discover -s tests -v", "granted"),
                ("ruff check src/other.py", "granted"),
                ("ruff format src/mine.py", "granted"),
                ("echo done > docs/notes/out.txt", "granted"),
                ("ls > /dev/null 2>&1", "granted"),
                ("cp src/other.py docs/notes/", "granted"),
                ("git add src/mine.py && git commit -m 'fix: x; y' -- src/mine.py", "granted"),
                ("cd src && cat mine.py", "granted"),
                ("find src -name x.py", "granted"),
            ],
            bash,
        )

    def test_destructive_verbs_escalate_whatever_the_grants(self):
        grants = (*ALL_ACTIONS, "delete", "version")
        self.check(
            [
                ("rm src/mine.py", "destructive"),
                ("rm -rf docs/notes", "destructive"),
                ("git push", "destructive"),
                ("git reset --hard", "destructive"),
                ("git checkout -- src/mine.py", "destructive"),
                ("git commit --amend -m x -- src/mine.py", "destructive"),
                ("git branch -D old", "destructive"),
                ("find docs -delete", "destructive"),
                ("cat src/mine.py; rm src/mine.py", "destructive"),
            ],
            lambda command: bash(command, grants),
        )

    def test_unknown_shells_and_unparsed_input_escalate(self):
        self.check(
            [
                ("", "unparsed"),
                ("echo 'unclosed", "unparsed"),
                ("bash -c 'cat x'", "shell"),
                ("fish -c ls", "shell"),
                ("eval ls", "shell"),
                ("python script.py", "shell"),
                ("find . -exec cat {} +", "shell"),
                ("rg --pre cat foo", "shell"),
                ("ls | xargs cat", "shell"),
                ("echo $(whoami)", "unsupported-syntax"),
                ("echo `id`", "unsupported-syntax"),
                ("cat $HOME/x", "unsupported-syntax"),
                ("ls\nrm -rf /", "unsupported-syntax"),
                ("sleep 1 &", "unsupported-syntax"),
                ("(cd /tmp && ls)", "unsupported-syntax"),
                ("cat <<EOF", "unsupported-syntax"),
                ("cat ~/secrets", "unsupported-syntax"),
                ("cp src/*.py docs/notes", "unsupported-syntax"),
                ("FOO=1 make test", "wrapper"),
                ("sudo ls", "wrapper"),
                ("git -C /tmp status", "wrapper"),
                ("curl https://example.com", "unknown-command"),
                ("make gate", "unknown-command"),
                ("sed -i s/a/b/ src/mine.py", "unknown-command"),
                ("git log --output=src/mine.py", "unknown-command"),
                ("git commit -a -m x", "unknown-command"),
            ],
            bash,
        )

    def test_paths_outside_the_grants_escalate(self):
        self.check(
            [
                ("cat /etc/passwd", "outside-grant"),
                ("cat ../other/file", "outside-grant"),
                ("cat .git/config", "outside-grant"),
                ("cd .. && ls", "outside-grant"),
                ("echo x > src/other.py", "outside-grant"),
                ("touch src/mine.py.bak", "outside-grant"),
                ("git add -A", "outside-grant"),
                ("git add .", "outside-grant"),
                ("git commit -m x", "outside-grant"),
                ("ruff format .", "outside-grant"),
                ("mv src/other.py src/mine.py", "outside-grant"),
                ("git add src/mine.py '>' src/other.py", "outside-grant"),
            ],
            bash,
        )
        self.assertEqual(bash("ls", cwd="/tmp").code, "outside-grant")

    def test_missing_action_grant_denies(self):
        self.check(
            [
                ("echo x > docs/notes/a", "action-not-granted"),
                ("make test", "action-not-granted"),
                ("git add src/mine.py", "action-not-granted"),
                ("touch docs/notes/a", "action-not-granted"),
                ("cat src/mine.py", "granted"),
            ],
            lambda command: bash(command, actions=()),
        )
        # Deny outranks escalate when one command of many is not granted.
        self.assertEqual(bash("rm x; make test", actions=()).code, "action-not-granted")

    def test_native_tools_are_judged_by_their_paths(self):
        def tool(name, paths, actions=ALL_ACTIONS):
            return decide(Request(name, PROJECT, PROJECT, OWNED, actions, paths=paths)).code

        self.assertEqual(tool("Read", ("/repo/src/other.py",)), "granted")
        self.assertEqual(tool("Grep", ()), "granted")
        self.assertEqual(tool("Read", ("/Users/x/.ssh/id_ed25519",)), "outside-grant")
        self.assertEqual(tool("Edit", ("/repo/src/mine.py",)), "granted")
        self.assertEqual(tool("Write", ("/repo/docs/notes/new.md",)), "granted")
        self.assertEqual(tool("Edit", ("/repo/src/other.py",)), "outside-grant")
        self.assertEqual(tool("Edit", ("/repo/src/mine.py",), ()), "action-not-granted")
        self.assertEqual(tool("Edit", ()), "unparsed")
        self.assertEqual(tool("WebFetch", ()), "unknown-tool")

    def test_crew_protocol_verbs_pass_but_captain_cannot_launder_other_work(self):
        captain = shlex.join([sys.executable, "-m", "captain_barbossa", "--session", "s1"])
        self.check(
            [
                (f"{captain} done Tia --report 'files; checks'", "granted"),
                (f"{captain} check Tia edit src/mine.py", "granted"),
                (f"{captain} ask Tia 'which file?' 2>&1", "granted"),
                (f"{captain} inbox Tia", "granted"),
                (f"{captain} memory show --scope repo", "granted"),
                (f"{captain} memory add Tia report 'summary'", "granted"),
                (f"{captain} memory add a decided b --because c --scope repo", "shell"),
                (f"{captain} do commit --message fix", "shell"),
                (f"{captain} crew --task x", "shell"),
                ("python3 -m captain_barbossa done Tia", "shell"),
                ("python3 -c 'import os'", "shell"),
                ("sh -c 'rm -rf beta.txt'", "shell"),
                ("sh -c 'curl https://example.com'", "shell"),
            ],
            bash,
        )
        # `python -m` imports from its cwd first; a crew-writable module there runs as captain.
        owned = ("src",)
        request = Request("Bash", PROJECT, f"{PROJECT}/src", owned, command=f"{captain} done Tia")
        self.assertEqual(decide(request).code, "shell")
        request = Request("Bash", PROJECT, PROJECT, owned, command=f"{captain} done Tia")
        self.assertEqual(decide(request).code, "granted")

    def test_argv_requests_unwrap_only_known_shells(self):
        def argv(*words):
            return decide(Request("Bash", PROJECT, PROJECT, OWNED, ALL_ACTIONS, argv=words)).code

        self.assertEqual(argv("bash", "-lc", "git status && make test"), "granted")
        self.assertEqual(argv("zsh", "-c", "rm -rf src"), "destructive")
        self.assertEqual(argv("fish", "-c", "ls"), "shell")
        self.assertEqual(argv("git", "status"), "granted")
        # An exec argv has no shell, so operators are plain arguments.
        self.assertEqual(argv("cat", ";", "src/mine.py"), "granted")


class ApprovalHookTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew("jack", {"name": "Jack", "incarnation_id": "first"}, self.current)
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)

    def hook(self, tool="Bash", inputs=None, **extra):
        event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(self.project),
            "tool_name": tool,
            "tool_input": inputs or {"command": "git status"},
            **extra,
        }
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps(event)])
        logged = json.loads(self.crew.events.read_text().splitlines()[-1])
        return json.loads(output.getvalue()), logged

    def test_allow_once_uses_native_response_and_does_not_leave_an_approval_notice(self):
        output, event = self.hook("Edit", {"file_path": "src/a.py"})
        self.assertEqual(
            output["hookSpecificOutput"],
            {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}},
        )
        self.assertEqual(event["approval"], {"verdict": "allow-once", "code": "granted"})
        self.assertIsNone(event_status(event))
        self.assertEqual(self.crew.current_state().state, "working")

    def test_denied_action_refuses_the_tool(self):
        output, event = self.hook(inputs={"command": "git commit src/a.py -m fix"})
        decision = output["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("action-not-granted", decision["message"])
        self.assertIsNone(event_status(event))

    def test_escalation_reaches_the_native_user_prompt_and_wait(self):
        for command in ("rm src/a.py", "python -c 'print(1)'", "cat /etc/passwd"):
            with self.subTest(command=command):
                output, event = self.hook(inputs={"command": command})
                self.assertNotIn("hookSpecificOutput", output)
                self.assertIn("User approval required", output["systemMessage"])
                self.assertEqual(event["approval"]["verdict"], "escalate")
                self.assertEqual(event_status(event), "blocked")

    def test_malformed_missing_and_stale_requests_never_allow(self):
        for extra in ({"tool_input": []}, {"cwd": None}, {"tool_input": {"command": []}}):
            with self.subTest(extra=extra):
                output, event = self.hook(**extra)
                self.assertNotIn("hookSpecificOutput", output)
                self.assertEqual(event["approval"]["code"], "unparsed")
        store.write_json(self.current.directory / "protocol.json", {})
        output, _ = self.hook()
        self.assertNotIn("hookSpecificOutput", output)
        old = self.crew.events.with_name("jack-old.jsonl")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(old), '{"hook_event_name": "PermissionRequest"}'])
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(old.exists())

    def test_symlink_tool_path_outside_the_project_escalates(self):
        (self.project / "src").mkdir()
        (self.project / "src" / "outside").symlink_to(self.project.parent, target_is_directory=True)
        output, event = self.hook("Edit", {"file_path": "src/outside/file.py"})
        self.assertNotIn("hookSpecificOutput", output)
        self.assertEqual(event["approval"]["code"], "outside-grant")

    def pretool(self, inputs, tool="Bash"):
        return self.hook(tool, inputs, hook_event_name="PreToolUse", tool_use_id="toolu_1")

    def test_pretooluse_enforces_grants_without_a_native_prompt(self):
        output, event = self.pretool({"file_path": "src/a.py"}, "Edit")
        self.assertEqual(
            output["hookSpecificOutput"],
            {"hookEventName": "PreToolUse", "permissionDecision": "allow"},
        )
        self.assertEqual(event["hook_event_name"], "PermissionDecision")
        output, _ = self.pretool({"command": "git commit src/a.py -m fix"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "action-not-granted", output["hookSpecificOutput"]["permissionDecisionReason"]
        )
        for command in ("rm -rf beta.txt", "curl https://example.com"):
            with self.subTest(command=command):
                output, event = self.pretool({"command": command})
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")
                self.assertEqual(event["hook_event_name"], "PreToolUse")
                self.assertEqual(self.crew.current_state().state, "working")

    def test_pretooluse_leaves_tools_without_policy_to_the_native_flow(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps({
                "hook_event_name": "PreToolUse", "cwd": str(self.project),
                "tool_name": "WebFetch", "tool_input": {"url": "https://example.com"},
            })])  # fmt: skip
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.crew.events.read_text(), "")

    def test_a_pretooluse_ask_is_not_decided_or_logged_again_by_permissionrequest(self):
        inputs = {"command": "rm -rf beta.txt"}
        self.pretool(inputs)
        with patch.object(events, "decide", side_effect=AssertionError("decided twice")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                events.append_event([str(self.crew.events), json.dumps({
                    "hook_event_name": "PermissionRequest", "cwd": str(self.project),
                    "tool_name": "Bash", "tool_input": inputs,
                })])  # fmt: skip
        self.assertEqual(output.getvalue(), "")
        logged = [json.loads(line) for line in self.crew.events.read_text().splitlines()]
        self.assertEqual(
            [e["hook_event_name"] for e in logged], ["PreToolUse", "PermissionRequest"]
        )
        self.assertEqual(logged[-1]["approval"]["pretool"], "toolu_1")
        self.assertNotIn("source", logged[-1]["approval"])
        self.assertEqual(event_status(logged[-1]), "blocked")
        # A second prompt for the same command is a new call, decided afresh.
        output, event = self.hook(inputs=inputs)
        self.assertIn("User approval required", output["systemMessage"])
        self.assertNotIn("pretool", event["approval"])

    def test_a_reprompted_pretooluse_allow_is_answered_without_a_second_record(self):
        inputs = {"file_path": "src/a.py"}
        self.pretool(inputs, "Edit")
        with patch.object(events, "decide", side_effect=AssertionError("decided twice")):
            output, _ = self.hook("Edit", inputs)
        self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        self.assertEqual(len(self.crew.events.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
