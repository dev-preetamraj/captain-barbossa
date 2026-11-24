"""Crew launch wiring: no native permission prompt, hooks deny flagged effects."""

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import instructions, pi_history
from tests.home_isolation import SessionCase


def codex_hooks(args):
    declared = {}
    for arg in args:
        if arg.startswith("hooks."):
            table = tomllib.loads(arg)["hooks"]
            declared.update(
                {event: hooks[0]["hooks"][0]["command"] for event, hooks in table.items()}
            )
    return declared


class CrewLaunchPermissionTests(SessionCase):
    def test_claude_crew_run_in_auto_mode_and_the_captain_keeps_its_own(self):
        crew = instructions.native_args("claude", "x", events="/tmp/events/jack-first.jsonl")
        self.assertEqual(crew[crew.index("--permission-mode") + 1], "auto")
        captain = instructions.native_args("claude", "x", events="/tmp/events/captain.jsonl")
        self.assertNotIn("--permission-mode", captain)

    def test_codex_crew_turn_off_the_update_check_and_have_no_reviewer(self):
        for supported in (False, True):
            with (
                self.subTest(supported=supported),
                patch("captain_barbossa.instructions.codex_stop_hooks", return_value=supported),
            ):
                crew = instructions.native_args("codex", "x", events="/tmp/jack-first.jsonl")
                self.assertIn("check_for_update_on_startup=false", crew)
                self.assertNotIn("approvals_reviewer", " ".join(crew))
                captain = instructions.native_args("codex", "x", events="/tmp/captain.jsonl")
                self.assertNotIn("check_for_update_on_startup=false", captain)

    def test_codex_registers_four_identical_hooks_only_above_the_floor(self):
        with patch("captain_barbossa.instructions.codex_stop_hooks", return_value=True):
            hooks = codex_hooks(
                instructions.native_args("codex", "x", events="/tmp/jack-first.jsonl")
            )
        self.assertEqual(set(hooks), {"SessionStart", "Stop", "PreToolUse", "PermissionRequest"})
        self.assertEqual(len(set(hooks.values())), 1)
        with patch("captain_barbossa.instructions.codex_stop_hooks", return_value=False):
            hooks = codex_hooks(
                instructions.native_args("codex", "x", events="/tmp/jack-first.jsonl")
            )
        self.assertEqual(set(hooks), {"SessionStart"})

    def test_python_command_pins_the_python3_sibling_spelling(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("python", "python3"):
                (Path(root) / name).touch()
            with patch.object(sys, "executable", str(Path(root) / "python")):
                self.assertEqual(instructions.python_command(), str(Path(root) / "python3"))
                self.assertEqual(
                    shlex.split(instructions.captain_command("s1"))[0], str(Path(root) / "python3")
                )
                hook = instructions.native_args("claude", "x", events="/tmp/jack-first.jsonl")
                settings = json.loads(hook[hook.index("--settings") + 1])
                command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                self.assertEqual(shlex.split(command)[0], str(Path(root) / "python3"))
            (Path(root) / "python3").unlink()
            with patch.object(sys, "executable", str(Path(root) / "python")):
                self.assertEqual(instructions.python_command(), str(Path(root) / "python"))

    def test_grok_crew_keep_auto_mode_and_register_no_hook(self):
        args = instructions.native_args("grok", "x", events="/tmp/jack-first.jsonl")
        self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
        self.assertNotIn("hook", " ".join(args))


class PiHookTests(unittest.TestCase):
    def test_extension_without_a_hook_pipes_nothing(self):
        self.assertNotIn("spawnSync(HOOK", pi_history.extension("/r", "/e", "i1"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pi_blocks_only_on_a_deny_from_the_hook(self):
        with tempfile.TemporaryDirectory() as root:
            hook = Path(root) / "hook.py"
            seen = Path(root) / "seen.json"
            hook.write_text(
                "import json, sys\n"
                "event = json.load(sys.stdin)\n"
                f"open({str(seen)!r}, 'w').write(json.dumps(event))\n"
                "if 'push' in json.dumps(event['tool_input']):\n"
                "    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',"
                " 'permissionDecision': 'deny', 'permissionDecisionReason': 'Flagged: leaves'}}))\n"
            )
            source = pi_history.extension(
                "/tmp/r", "/tmp/e.jsonl", "i1", hook=[sys.executable, str(hook)]
            )
            harness = (
                source.replace("export default function", "const main = function")
                + """
const handlers = {};
main({ on: (name, fn) => { handlers[name] = fn; } });
const ctx = { cwd: '/w', sessionManager: { getSessionId: () => 's1' } };
const call = (command, id) => handlers.tool_call(
  { toolName: 'bash', toolCallId: id, input: { command } }, ctx);
const deny = await call('git push', 't1');
const pass = await call('git status', 't2');
console.log(JSON.stringify([deny, pass ?? null]));
"""
            )
            done = subprocess.run(
                ["node", "--input-type=module", "-e", harness],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            deny, allowed = json.loads(done.stdout)
            self.assertEqual(deny, {"block": True, "reason": "Flagged: leaves"})
            self.assertIsNone(allowed)
            self.assertEqual(
                json.loads(seen.read_text()),
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "bash",
                    "tool_input": {"command": "git status"},
                    "tool_use_id": "t2",
                    "session_id": "s1",
                    "cwd": "/w",
                },
            )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pi_blocks_when_the_hook_fails(self):
        scripts = {
            "missing": None,
            "exit": "import sys; sys.exit(3)",
            "garbage": "print('not json')",
        }
        with tempfile.TemporaryDirectory() as root:
            for name, body in scripts.items():
                hook = Path(root) / f"{name}.py"
                argv = [sys.executable, str(hook)]
                if name == "missing":
                    argv = [str(Path(root) / "absent")]
                else:
                    hook.write_text(body or "pass")
                source = pi_history.extension("/tmp/r", "/tmp/e.jsonl", "i1", hook=argv)
                harness = (
                    source.replace("export default function", "const main = function")
                    + """
const handlers = {};
main({ on: (name, fn) => { handlers[name] = fn; } });
const ctx = { cwd: '/w', sessionManager: { getSessionId: () => 's1' } };
console.log(JSON.stringify(await handlers.tool_call(
  { toolName: 'bash', toolCallId: 't', input: { command: 'ls' } }, ctx) ?? null));
"""
                )
                with self.subTest(name=name):
                    done = subprocess.run(
                        ["node", "--input-type=module", "-e", harness],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(done.returncode, 0, done.stderr)
                    verdict = json.loads(done.stdout)
                    self.assertTrue(verdict["block"])
                    self.assertIn("Captain hook failed", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
