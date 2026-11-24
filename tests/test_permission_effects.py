import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import events, protocol, sessions, store
from captain_barbossa.approval import Request, Verdict, decide
from captain_barbossa.crew import Crew, event_status
from tests.home_isolation import SessionCase

PROJECT = "/repo"


def shell(command, owned=("src",), actions=("edit",), **extra):
    return decide(Request("Bash", PROJECT, PROJECT, owned, actions, command=command, **extra))


class EffectClassTests(unittest.TestCase):
    def test_each_risky_effect_has_its_class(self):
        for command, code in (
            ("git push origin main", "flagged-outward"),
            ("git push --force origin main", "flagged-history"),
            ("git push -fq origin main", "flagged-history"),
            ("git push origin +main", "flagged-history"),
            ("git rebase -i HEAD~3", "flagged-history"),
            ("git reset HEAD~1", "flagged-history"),
            ("git reset --hard", "flagged-loss"),
            ("git clean -fdx", "flagged-loss"),
            ("git stash", "flagged-loss"),
            ("git switch other", "flagged-loss"),
            ("git merge other", "flagged-loss"),
            ("git commit -am fix", "flagged-loss"),
            ("git tag -d v1", "flagged-history"),
            ("npm install left-pad", "flagged-outward"),
            ("uv add requests", "flagged-outward"),
            ("kubectl apply -f x.yaml", "flagged-outward"),
            ("sudo ls", "flagged-boundary"),
            ("chmod +x src/run.sh", "flagged-boundary"),
            ("git worktree add ../x", "flagged-boundary"),
        ):
            with self.subTest(command=command):
                self.assertEqual(shell(command, actions=("edit", "commit")).code, code)

    def test_local_reads_builds_and_tests_stay_quiet(self):
        for command in ("npm test", "cargo build", "kubectl get pods", "pip list"):
            with self.subTest(command=command):
                self.assertNotEqual(shell(command).verdict, Verdict.DENY)

    def test_api_words_are_scanned_only_inside_interpreter_payloads(self):
        # An unlexed grep for a word like "rename" is a read, not an inline write.
        self.assertEqual(shell('grep -rn "rename" src $(echo x)').code, "unsupported-syntax")
        self.assertEqual(shell("grep -n remove src\nwc -l src").code, "unsupported-syntax")
        self.assertEqual(
            shell("python3 - <<'EOF'\nimport os; os.rename('a', 'b')\nEOF").code, "inline-write"
        )
        self.assertEqual(shell("node -e \"fetch('x')\" $X").code, "flagged-outward")
        # The command-word scan still covers every unlexed text.
        self.assertEqual(shell("echo $(git push)").code, "flagged-outward")

    def test_captain_interpreter_sibling_is_accepted_only_outside_the_checkout(self):
        directory = os.path.dirname(sys.executable)
        for name in ("python", "python3"):
            sibling = os.path.join(directory, name)
            with self.subTest(name=name):
                command = shlex.join([sibling, "-m", "captain_barbossa", "inbox", "Tia"])
                request = Request("Bash", PROJECT, PROJECT, ("src",), command=command)
                self.assertEqual(decide(request).code, "granted")
                inside = Request("Bash", directory, directory, ("x",), command=command)
                expected = "granted" if sibling == sys.executable else "captain-verb"
                self.assertEqual(decide(inside).code, expected)
        other = shlex.join(["/opt/python3", "-m", "captain_barbossa", "inbox", "Tia"])
        self.assertEqual(shell(other).code, "captain-verb")

    def test_writes_outside_the_checkout_are_flagged_but_temp_is_scratch(self):
        scratch = {"scratch": ("/scratch",), "fenced": ("/scratch/captain-barbossa-1",)}
        self.assertEqual(shell("echo x > /scratch/out.txt", **scratch).code, "granted")
        self.assertEqual(
            shell("echo x > /scratch/captain-barbossa-1/s/protocol.json", **scratch).code,
            "flagged-boundary",
        )
        self.assertEqual(shell("echo x > /elsewhere/out.txt", **scratch).code, "flagged-boundary")
        self.assertEqual(shell("echo x > other.py", **scratch).code, "outside-grant")
        # A read-only crew is denied the write with its reason, never prompted.
        self.assertEqual(
            shell("echo x > /scratch/out.txt", actions=(), **scratch).code, "action-not-granted"
        )

    def test_protected_checkout_files_are_flagged_not_claimable(self):
        for path in ("Makefile", "AGENTS.md", "CLAUDE.md", ".envrc", ".github/workflows/ci.yml"):
            with self.subTest(path=path):
                request = Request(
                    "Write", PROJECT, PROJECT, ("src",), ("edit",), paths=(f"/repo/{path}",)
                )
                self.assertEqual(decide(request).code, "flagged-boundary")
        owned = Request(
            "Write", PROJECT, PROJECT, ("Makefile",), ("edit",), paths=("/repo/Makefile",)
        )
        self.assertEqual(decide(owned).code, "granted")

    def test_protected_files_under_an_owned_directory_stay_flagged(self):
        def write(owned, path):
            return decide(Request("Write", PROJECT, PROJECT, owned, ("edit",), paths=(path,)))

        for path in ("src/AGENTS.md", "src/Makefile", "src/.claude/x.json", "src/.envrc"):
            with self.subTest(path=path):
                self.assertEqual(write(("src",), f"/repo/{path}").code, "flagged-boundary")
                self.assertEqual(shell(f"echo x > {path}").code, "flagged-boundary")
        self.assertEqual(write(("src/Makefile",), "/repo/src/Makefile").code, "granted")
        ci = "/repo/.github/workflows/ci.yml"
        self.assertEqual(write((".github/workflows",), ci).code, "granted")
        self.assertEqual(write((".github",), ci).code, "flagged-boundary")

    def test_unlexable_shell_still_has_its_write_targets_judged(self):
        # A read-only crew never writes through a variable-expanding command.
        self.assertEqual(shell("echo $X > out.txt", actions=()).code, "action-not-granted")
        self.assertEqual(shell("echo $X | tee src/a.py", actions=()).code, "action-not-granted")
        self.assertEqual(shell("echo $X > other.py").code, "outside-grant")
        self.assertEqual(shell('echo x > "$OUT"').code, "outside-grant")
        self.assertEqual(shell("cp $SRC /elsewhere/x").code, "flagged-boundary")
        self.assertEqual(shell("cat <<EOF > /elsewhere/x\n$Y\nEOF").code, "flagged-boundary")
        # Owned targets and data inside a heredoc body keep the native decision.
        self.assertEqual(shell("echo $X > src/a.py 2>&1").code, "unsupported-syntax")
        heredoc = "python3 - <<'EOF'\nif a > b: print(a)\nEOF"
        self.assertEqual(shell(heredoc).code, "unsupported-syntax")
        self.assertEqual(shell("echo $X > /dev/null", actions=()).code, "unsupported-syntax")


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        (self.root / "src").mkdir()
        for name in ("clean.py", "dirty.py"):
            (self.root / "src" / name).write_text("x\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        (self.root / "src" / "dirty.py").write_text("uncommitted\n")
        (self.root / "src" / "new.py").write_text("untracked\n")
        (self.root / "other.py").write_text("not owned\n")

    def judge(self, command, **extra):
        fields = {"owned": ("src",), "actions": ("edit",), **extra}
        request = Request("Bash", str(self.root), str(self.root), command=command, **fields)
        return events.judged(request)

    def test_owned_deletes_pass_only_when_recoverable(self):
        self.assertEqual(self.judge("rm src/clean.py").code, "granted")
        self.assertEqual(self.judge("git rm src/clean.py").code, "granted")
        self.assertEqual(self.judge("rm src/missing.py").code, "granted")
        for command in ("rm src/dirty.py", "rm src/new.py", "rm -rf src", "rm other.py"):
            with self.subTest(command=command):
                self.assertEqual(self.judge(command).code, "flagged-loss")

    def test_files_this_assignment_created_may_be_deleted(self):
        created = (str(self.root / "src" / "new.py"),)
        self.assertEqual(self.judge("rm src/new.py", recoverable=created).code, "granted")

    def test_the_delete_grant_covers_granted_paths_but_never_claimed_ones(self):
        actions = ("edit", "delete")
        self.assertEqual(self.judge("rm src/dirty.py", actions=actions).code, "granted")
        claimed = self.judge("rm src/dirty.py", actions=actions, claimed=("src/dirty.py",))
        self.assertEqual(claimed.code, "flagged-loss")

    def test_replacing_a_dirty_file_is_flagged_unless_the_captain_assigned_it(self):
        claimed = {
            "owned": ("src/dirty.py", "src/clean.py"),
            "claimed": ("src/dirty.py", "src/clean.py"),
        }
        dirty, clean = str(self.root / "src" / "dirty.py"), str(self.root / "src" / "clean.py")

        def native(tool, path, **extra):
            fields = {"actions": ("edit",), "paths": (path,), **extra}
            return events.judged(Request(tool, str(self.root), str(self.root), **fields)).code

        for command in (
            "echo x > src/dirty.py",
            "echo x | tee src/dirty.py",
            "cp other.py src/dirty.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.judge(command, **claimed).code, "flagged-loss")
                self.assertEqual(self.judge(command).code, "granted")
        self.assertEqual(self.judge("echo x >> src/dirty.py", **claimed).code, "granted")
        self.assertEqual(self.judge("echo x > src/clean.py", **claimed).code, "granted")
        self.assertEqual(native("Write", dirty, **claimed), "flagged-loss")
        self.assertEqual(native("Write", dirty, owned=("src",)), "granted")
        self.assertEqual(native("Write", clean, **claimed), "granted")
        self.assertEqual(native("Edit", dirty, **claimed), "granted")
        # Codex apply_patch: an Add File or Move target over an existing file replaces it.
        self.assertEqual(native("Edit", dirty, replaces=(dirty,), **claimed), "flagged-loss")
        patch_text = (
            "*** Begin Patch\n*** Update File: a\n*** Move to: b\n"
            "*** Add File: c\n+x\n*** End Patch\n"
        )
        call = events.native_call("codex", "apply_patch", {"command": patch_text})
        self.assertEqual(call[-1], ("c", "b"))

    def test_moves_delete_their_sources(self):
        self.assertEqual(self.judge("mv src/clean.py src/renamed.py").code, "granted")
        self.assertEqual(self.judge("git mv src/clean.py src/renamed.py").code, "granted")
        self.assertEqual(self.judge("mv src/dirty.py src/renamed.py").code, "flagged-loss")
        self.assertEqual(self.judge("mv src/new.py src/clean.py").code, "flagged-loss")
        self.assertEqual(self.judge("mv -n src/new.py src/clean.py").code, "flagged-loss")


CODEX_PRETOOL = {
    # codex-rs/hooks/src/schema.rs:279-297 (rust-v0.172.0), PreToolUseCommandInput.
    "session_id": "019a-codex",
    "turn_id": "turn-1",
    "transcript_path": None,
    "cwd": "",
    "hook_event_name": "PreToolUse",
    "model": "gpt-5.5",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "git status"},
    "tool_use_id": "call_1",
}


class ProviderPayloadTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "incarnation_id": "first", "provider": "codex"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)

    def provider(self, name):
        self.crew.record["provider"] = name
        store.write_json(self.current.meta_path, self.current.meta)

    def send(self, event):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps(event)])
        logged = json.loads(self.crew.events.read_text().splitlines()[-1])
        return json.loads(output.getvalue() or "{}"), logged

    def codex(self, hook, tool, inputs, **extra):
        event = {**CODEX_PRETOOL, "cwd": str(self.project), "hook_event_name": hook}
        event.update(tool_name=tool, tool_input=inputs, **extra)
        if hook == "PermissionRequest":
            # schema.rs:302-319: PermissionRequestCommandInput has no tool_use_id.
            event.pop("tool_use_id")
        return self.send(event)

    def test_codex_pretooluse_denies_flags_and_stays_silent_on_allow(self):
        output, event = self.codex("PreToolUse", "Bash", {"command": "git status"})
        self.assertEqual(output, {})
        self.assertEqual(event["approval"]["code"], "granted")
        output, event = self.codex("PreToolUse", "Bash", {"command": "git push"})
        native = output["hookSpecificOutput"]
        self.assertEqual(native["permissionDecision"], "deny")
        self.assertIn("Flagged:", native["permissionDecisionReason"])
        self.assertEqual(event["hook_event_name"], "PermissionDecision")

    def test_codex_permission_request_is_total(self):
        # core/src/tools/sandboxing.rs:142-158 adds an optional description.
        inputs = {"command": "make test", "description": "run tests"}
        self.codex("PermissionRequest", "Bash", inputs)
        state = store.read_json(self.current.directory / "protocol.json")
        state["assignments"][state["active"]["jack"]]["actions"].append("test")
        store.write_json(self.current.directory / "protocol.json", state)
        output, _ = self.codex("PermissionRequest", "Bash", inputs)
        self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        output, event = self.codex("PermissionRequest", "Bash", {"command": "make gate"})
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIsNone(event_status(event))

    def test_codex_argv_commands_are_judged_as_argv(self):
        output, event = self.codex("PreToolUse", "Bash", {"command": ["bash", "-lc", "git push"]})
        self.assertEqual(event["approval"]["code"], "flagged-outward")
        _, event = self.codex("PreToolUse", "Bash", {"command": ["git", "status"]})
        self.assertEqual(event["approval"]["code"], "granted")

    def test_codex_apply_patch_paths_are_judged_and_claimed(self):
        # core/src/tools/handlers/apply_patch.rs:370-374: the raw patch is tool_input.command.
        patch_text = (
            "*** Begin Patch\n*** Add File: src/new.py\n+x\n"
            "*** Update File: notes.md\n@@\n-a\n+b\n*** End Patch\n"
        )
        output, event = self.codex("PreToolUse", "apply_patch", {"command": patch_text})
        self.assertEqual(output, {})
        self.assertEqual(event["approval"]["code"], "granted")
        assignment = protocol.active_assignment(self.crew)
        self.assertIn("notes.md", assignment["paths"])
        # Intent only: the patch has not run, so nothing is created yet.
        self.assertEqual(assignment["creating"][0]["path"], str(self.project / "src" / "new.py"))
        self.assertNotIn("created", assignment)
        (self.project / "src").mkdir()
        (self.project / "src" / "kept.py").write_text("someone's\n")
        delete = "*** Begin Patch\n*** Delete File: src/kept.py\n*** End Patch\n"
        _, event = self.codex("PreToolUse", "apply_patch", {"command": delete})
        self.assertEqual(event["approval"]["code"], "flagged-loss")
        protected_patch = "*** Begin Patch\n*** Update File: Makefile\n@@\n-a\n+b\n*** End Patch\n"
        _, event = self.codex("PreToolUse", "apply_patch", {"command": protected_patch})
        self.assertEqual(event["approval"]["code"], "flagged-boundary")
        _, event = self.codex("PreToolUse", "apply_patch", {"command": "not a patch"})
        self.assertEqual(event["approval"]["code"], "unparsed")

    def test_codex_session_start_and_stop_keep_their_own_handling(self):
        with patch.object(events, "permission_decision") as permission:
            output, _ = self.send({"hook_event_name": "Stop", "session_id": "s"})
        permission.assert_not_called()
        self.assertEqual(output["decision"], "block")

    def test_pi_lowercase_tools_without_cwd_are_judged_from_the_execution_root(self):
        self.provider("pi")

        def pi(tool, inputs):
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": inputs,
                "tool_use_id": "pi-1",
            }
            return self.send(event)

        output, event = pi("write", {"path": "src/a.py", "content": "x"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(event["approval"]["code"], "granted")
        output, _ = pi("bash", {"command": "git push"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        _, event = pi("read", {"path": "/etc/hosts"})
        self.assertEqual(event["approval"]["code"], "granted")
        _, event = pi("edit", {"path": "Makefile", "edits": []})
        self.assertEqual(event["approval"]["code"], "flagged-boundary")

    def test_an_unmanaged_codex_captain_is_never_gated(self):
        path = self.current.events("captain")
        event = {**CODEX_PRETOOL, "cwd": str(self.project), "tool_input": {"command": "git push"}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(path), json.dumps(event)])
        self.assertEqual(output.getvalue(), "")

    def test_a_claude_captain_keeps_its_native_permission_prompt(self):
        self.current.meta["captain_provider"] = "claude"
        store.write_json(self.current.meta_path, self.current.meta)
        path = self.current.events("captain")
        for hook in ("PreToolUse", "PermissionRequest"):
            with self.subTest(hook=hook):
                event = {
                    "hook_event_name": hook,
                    "session_id": "captain",
                    "tool_use_id": "toolu_c",
                    "cwd": str(self.project),
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push"},
                }
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    events.append_event([str(path), json.dumps(event)])
                self.assertEqual(output.getvalue(), "")
                logged = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual(logged["hook_event_name"], hook)
                self.assertNotIn("approval", logged)

    def pi(self, tool, inputs):
        event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inputs}
        return self.send({**event, "tool_use_id": "pi-1", "cwd": str(self.project)})

    def test_pi_paths_are_resolved_as_pi_resolves_them(self):
        self.provider("pi")
        with patch.dict(os.environ, {"HOME": "/nonexistent-home"}):
            _, event = self.pi("write", {"path": "~/notes.md", "content": "x"})
        self.assertEqual(event["approval"]["code"], "flagged-boundary")
        _, event = self.pi("write", {"path": "@notes.md", "content": "x"})
        self.assertEqual(event["approval"]["code"], "granted")
        paths = protocol.active_assignment(self.crew)["paths"]
        self.assertIn("notes.md", paths)
        self.assertFalse({"~/notes.md", "@notes.md"} & set(paths))
        call = events.native_call("pi", "edit", {"path": "@src/a\u202fb.py"})
        self.assertEqual(call[1], ["src/a b.py"])
        _, event = self.pi("write", {"path": "file://elsewhere/x.py", "content": "x"})
        self.assertEqual(event["approval"]["code"], "flagged-boundary")

    def test_pi_powershell_is_judged_as_a_shell(self):
        self.provider("pi")
        output, event = self.pi("powershell", {"command": "git push"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(event["approval"]["code"], "flagged-outward")

    def test_a_creation_counts_only_once_its_call_ran_and_the_file_is_unchanged(self):
        self.provider("claude")
        (self.project / "src").mkdir()
        new = self.project / "src" / "new.py"

        def claude(hook, call, inputs=None):
            tool = "Bash" if inputs else "Write"
            inputs = inputs or {"file_path": "src/new.py", "content": "x"}
            event = {"hook_event_name": hook, "session_id": "s", "tool_use_id": call}
            return self.send(
                {**event, "cwd": str(self.project), "tool_name": tool, "tool_input": inputs}
            )

        def created():
            return events.trusted_created(protocol.active_assignment(self.crew))

        claude("PreToolUse", "t1")
        new.write_text("the user's\n")  # the Write failed; someone else made the file
        claude("PostToolUseFailure", "t1")
        self.assertEqual(created(), ())
        _, event = claude("PreToolUse", "t2", {"command": "rm src/new.py"})
        self.assertEqual(event["approval"]["code"], "flagged-loss")
        new.unlink()
        claude("PreToolUse", "t3")
        new.write_text("x\n")
        claude("PostToolUse", "t3")
        self.assertEqual(created(), (str(new),))
        # Replaced under the same name, it is no longer the file this assignment made.
        (self.project / "src" / "tmp").write_text("replaced\n")
        os.replace(self.project / "src" / "tmp", new)
        self.assertEqual(created(), ())
        _, event = claude("PreToolUse", "t4", {"command": "rm src/new.py"})
        self.assertEqual(event["approval"]["code"], "flagged-loss")
        # Codex has no post-tool hook: its next event settles, and a missing file is dropped.
        self.provider("codex")
        add = {"command": "*** Begin Patch\n*** Add File: src/lazy.py\n+x\n*** End Patch\n"}
        self.codex("PreToolUse", "apply_patch", add)
        self.send({"hook_event_name": "Stop", "session_id": "s"})
        self.assertEqual(protocol.active_assignment(self.crew)["creating"], [])
        self.codex("PreToolUse", "apply_patch", add)
        (self.project / "src" / "lazy.py").write_text("x\n")
        self.send({"hook_event_name": "Stop", "session_id": "s"})
        self.assertEqual(created(), (str(self.project / "src" / "lazy.py"),))

    def test_an_edit_only_writer_builds_and_tests_with_no_prompt(self):
        captain = shlex.join([sys.executable, "-m", "captain_barbossa", "memory", "add"])
        for command in (
            "make test",
            "uv run pytest -q",
            "python3 -m unittest discover -s tests",
            "npm test",
            "npm run build",
            "cargo build",
            "go vet ./...",
            "ruff format src",
            f"{captain} Jack note done",
        ):
            with self.subTest(command=command):
                output, _ = self.codex("PermissionRequest", "Bash", {"command": command})
                self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        output, _ = self.codex("PermissionRequest", "Bash", {"command": "ruff format"})
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")


if __name__ == "__main__":
    unittest.main()
