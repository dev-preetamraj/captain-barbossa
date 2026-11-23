"""Plugin-declared commands are denied to crew on every provider, launch path and alias."""

import contextlib
import io
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from captain_barbossa import (
    agents,
    approval,
    cli,
    do,
    events,
    pi_history,
    protocol,
    sessions,
    store,
)
from captain_barbossa.approval import Verdict
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase

DENIALS = [["example", "proposals", "approve"], ["example_cli", "proposals", "approve"]]

DENIED = (
    "example proposals approve 42",
    "/usr/local/bin/example proposals approve 42",
    "./bin/example --profile dev proposals approve 42",
    "env EXAMPLE_TOKEN=x example proposals approve 42",
    "uv run example proposals approve",
    "uvx example proposals approve",
    "uv tool run example proposals approve",
    "pipx run example proposals approve",
    "xargs example proposals approve < ids",
    "sh -c 'example proposals approve 42'",
    'bash -lc "cd /tmp && example proposals approve 42"',
    "git status && example proposals approve 42",
    "true | example proposals approve",
    "echo $(example proposals approve 42)",
    "`example proposals approve`",
    "python -m example_cli proposals approve 42",
    "python3 -m example_cli.main proposals approve",
    "a=example; $a proposals approve",
    "example.exe proposals approve",
    "nohup example proposals approve &",
    "e'x'ample proposals approve",
    'ex"am"ple proposals approve',
    "ex\\ample proposals approve",
    "example proposals appr''ove",
    "eval 'exa''mple proposals approve'",
    "EXAMPLE proposals Approve",
    "/usr/local/bin/exampl? proposals approve",
    "/opt/bin/ex*le proposals approve",
    "./[e]xample proposals approve",
)

ALLOWED = (
    "example proposals list",
    "example approve",
    "proposals approve example",
    "git status",
    "examples proposals approve",
    "cat README.md",
    "ls * proposals approve",
    "cat *.md proposals approve",
)


class DenialMatcherTests(unittest.TestCase):
    def test_every_alias_is_denied_and_unrelated_commands_pass(self):
        for command in DENIED:
            with self.subTest(command=command):
                self.assertIsNotNone(approval.denied(DENIALS, command))
                self.assertIsNotNone(approval.denied(DENIALS, ["bash", "-lc", command]))
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(approval.denied(DENIALS, command))
        self.assertIsNone(approval.denied([], "example proposals approve"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pi_gate_matches_exactly_like_the_python_matcher(self):
        source = pi_history.gate(DENIALS)
        harness = (
            source
            + """
const results = JSON.parse(process.argv[1]).map((c) => denied(c) !== null);
console.log(JSON.stringify(results));
"""
        )
        commands = [*DENIED, *ALLOWED]
        done = subprocess.run(
            ["node", "--input-type=module", "-e", harness, json.dumps(commands)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [True] * len(DENIED) + [False] * len(ALLOWED))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pi_extension_blocks_a_denied_bash_call(self):
        source = pi_history.extension("/tmp/r", "/tmp/e.jsonl", "i1", DENIALS)
        harness = (
            source.replace("export default function", "const main = function")
            + """
const handlers = {};
main({ on: (name, fn) => { handlers[name] = fn; } });
const deny = await handlers.tool_call({ toolName: 'bash', input: { command: 'uvx example proposals approve' } });
const pass = await handlers.tool_call({ toolName: 'bash', input: { command: 'git status' } });
console.log(JSON.stringify([deny, pass ?? null]));
"""
        )
        done = subprocess.run(
            ["node", "--input-type=module", "-e", harness],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        deny, allowed = json.loads(done.stdout)
        self.assertTrue(deny["block"])
        self.assertIn("plugin", deny["reason"])
        self.assertIsNone(allowed)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_declared_words_cannot_reach_the_gate_placeholders(self):
        rules = [["__BREAKS__", "__QUOTING__", "__RULES__"]]
        harness = pi_history.gate(rules) + "console.log(JSON.stringify(denied(process.argv[1])));"
        done = subprocess.run(
            ["node", "--input-type=module", "-e", harness, " ".join(rules[0])],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), rules[0])

    def test_extension_without_denials_has_no_gate(self):
        source = pi_history.extension("/tmp/r", "/tmp/e.jsonl", "i1")
        self.assertNotIn("tool_call", source)


class ClaudeHookDenialTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "incarnation_id": "first",
                "provider": "claude",
                "plugins": {"denials": DENIALS, "generations": {}},
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(self.crew, self.project, "run tests", ["src"], ["edit", "test"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch("captain_barbossa.crew.Pane.agent_status", return_value="working"))

    def hook(self, command, kind="PreToolUse", tool="Bash"):
        event = {
            "hook_event_name": kind,
            "session_id": "native",
            "tool_use_id": "toolu_1",
            "cwd": str(self.project),
            "tool_name": tool,
            "tool_input": {"command": command},
        }
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps(event)])
        return json.loads(output.getvalue() or "{}")

    def test_pretooluse_and_permission_request_deny_every_alias(self):
        for command in DENIED:
            with self.subTest(command=command):
                native = self.hook(command)["hookSpecificOutput"]
                self.assertEqual(native["permissionDecision"], "deny")
                self.assertIn("plugin-denied", native["permissionDecisionReason"])
                decision = self.hook(command, "PermissionRequest")["hookSpecificOutput"]["decision"]
                self.assertEqual(decision["behavior"], "deny")

    def test_any_tool_with_a_command_input_is_checked(self):
        native = self.hook("example proposals approve", tool="mcp__shell__run")
        self.assertEqual(native["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_denial_outranks_grants_and_a_finished_assignment(self):
        with protocol.checkpoint(self.current.directory) as state:
            assignment = protocol.active(state, self.crew)
            assignment["state"] = "done"
        native = self.hook("example proposals approve")["hookSpecificOutput"]
        self.assertIn("plugin-denied", native["permissionDecisionReason"])

    def test_session_denials_apply_to_a_crew_record_without_a_binding(self):
        self.crew.record.pop("plugins")
        self.current.meta["plugins"] = {"active": [], "denials": DENIALS}
        store.write_json(self.current.meta_path, self.current.meta)
        native = self.hook("example proposals approve")["hookSpecificOutput"]
        self.assertEqual(native["permissionDecision"], "deny")

    def test_unrelated_commands_reach_the_existing_gate_unchanged(self):
        self.assertEqual(approval.REASONS["plugin-denied"][0], Verdict.DENY)
        native = self.hook("git status")["hookSpecificOutput"]
        self.assertEqual(native["permissionDecision"], "allow")


class LaunchPathMatrixTests(SessionCase):
    """Foreground, background and quiet for each provider while a denial is active."""

    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def bind(self, provider, path, denials=DENIALS, managed=True):
        meta = {"plugins": {"active": [], "denials": denials}}
        record = {"id": "jack", "incarnation_id": "i1", "provider": provider}
        agents.bind_plugins(meta, record, provider, path, self.current.directory, managed=managed)
        return record

    def test_crew_recruitment_matrix(self):
        outcomes = {
            ("claude", "foreground"): "gated",
            ("pi", "foreground"): "gated",
            ("codex", "foreground"): "refused",
            ("grok", "foreground"): "refused",
            ("pi", "background"): "no-shell",
            ("codex", "background"): "refused",
        }
        for (provider, path), outcome in outcomes.items():
            with self.subTest(provider=provider, path=path):
                if outcome == "refused":
                    with self.assertRaises(store.CaptainError) as caught:
                        self.bind(provider, path)
                    message = str(caught.exception)
                    self.assertIn(provider, message)
                    self.assertIn("example", message)
                    self.assertNotIn("\n", message)
                else:
                    record = self.bind(provider, path)
                    self.assertEqual(record["plugins"]["denials"], DENIALS)
                # With no denial active, every path is unchanged.
                record = self.bind(provider, path, denials=[])
                self.assertEqual(record["plugins"]["denials"], [])

    def test_pi_crew_without_the_managed_extension_is_refused(self):
        with self.assertRaises(store.CaptainError):
            self.bind("pi", "foreground", managed=False)

    def test_pi_foreground_crew_extension_carries_the_gate(self):
        from captain_barbossa import native_history

        crew = Crew(
            "jack",
            {
                "id": "jack",
                "incarnation_id": "i1",
                "provider": "pi",
                "plugins": {"denials": DENIALS},
            },
            self.current,
        )
        history = native_history.prepare(crew, incarnation="i1", project=self.project)
        extension = history["argv"][history["argv"].index("--extension") + 1]
        source = open(extension, encoding="utf-8").read()
        self.assertIn("tool_call", source)
        self.assertIn("example_cli", source)

    def quiet(self, provider, *extra):
        store.write_json(self.current.directory / "captain.json", {"provider": provider})
        self.current.meta["plugins"] = {"active": [], "denials": DENIALS}
        store.write_json(self.current.meta_path, self.current.meta)
        args = cli.parser().parse_args(["quiet", "--task", "look", *extra])
        seen = {}

        def capture(root, command, timeout):
            seen["argv"] = command
            raise store.CaptainError("captured")

        with (
            patch.object(do, "executable", return_value=f"/bin/{provider}"),
            patch.object(do, "resolve_model", return_value="model"),
            patch.object(do, "_capture", side_effect=capture),
            self.assertRaises(store.CaptainError) as caught,
        ):
            do._quiet(args, self.project, self.current.directory)
        return seen.get("argv"), str(caught.exception)

    def test_quiet_matrix(self):
        argv, message = self.quiet("codex")
        self.assertIsNone(argv)
        self.assertIn("codex", message)
        self.assertIn("example", message)
        argv, _ = self.quiet("claude")
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Bash", argv[argv.index("--disallowedTools") :])
        argv, _ = self.quiet("claude", "--write", "src/a.py")
        self.assertNotIn("Bash", argv)
        argv, _ = self.quiet("grok")
        self.assertEqual(argv[argv.index("--deny") + 1], "Bash")
        argv, message = self.quiet("pi")
        self.assertIsNotNone(argv, message)
        self.assertIn("--no-extensions", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "read")

    def test_quiet_without_a_denial_is_unchanged(self):
        store.write_json(self.current.directory / "captain.json", {"provider": "codex"})
        args = cli.parser().parse_args(["quiet", "--task", "look"])
        with (
            patch.object(do, "executable", return_value="/bin/codex"),
            patch.object(do, "_capture", side_effect=store.CaptainError("captured")),
            self.assertRaises(store.CaptainError) as caught,
        ):
            do._quiet(args, self.project, self.current.directory)
        self.assertEqual(str(caught.exception), "captured")

    def test_captain_is_never_denied(self):
        events_path = self.current.events("captain")
        self.assertIsNone(
            events.plugin_denial(
                events_path, {"tool_input": {"command": "example proposals approve"}}
            )
        )
