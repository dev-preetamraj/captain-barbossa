import contextlib
import io
import json
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, runtime


class CaptainFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            )
        )
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_rejects_outside_herdr_without_contacting_server(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(runtime.CaptainError, "Herdr workspace"):
                runtime.current_pane()
            api.assert_not_called()

    def test_pane_run_accepts_empty_stdout_when_output_is_not_expected(self):
        with (
            patch.object(runtime, "executable", return_value="/bin/herdr"),
            patch.object(
                runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            self.assertEqual(
                runtime.herdr("pane", "run", "w1:p2", "echo hello", expect_output=False), {}
            )

    def test_unexpected_herdr_response_names_command_and_includes_output(self):
        for stdout, options in (
            ("", {}),
            ("", {"expect_output": True}),
            ("not JSON\n", {"expect_output": False}),
            ('{"result": null}\n', {}),
            ("{}\n", {}),
            ("[]\n", {}),
        ):
            with (
                self.subTest(stdout=stdout, options=options),
                patch.object(runtime, "executable", return_value="/bin/herdr"),
                patch.object(
                    runtime.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout, "CLI diagnostic"),
                ),
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "unexpected response") as error:
                    runtime.herdr("pane", "run", "w1:p2", "echo hello", **options)
                self.assertIn("herdr pane run", str(error.exception))
                self.assertIn(
                    f"Raw stdout:\n{stdout}\nStderr:\nCLI diagnostic", str(error.exception)
                )

    def test_instructions_delegate_routine_crew_approvals_to_captain(self):
        instructions = " ".join(agents.agent_instructions(self.directory, "captain").split())
        self.assertIn("captain memory reads/writes without asking the user", instructions)
        self.assertIn("herdr agent send-keys <name> y", instructions)
        self.assertIn('choose "don\'t ask again" when available', instructions)
        self.assertIn("Escalate only destructive commands", instructions)
        self.assertIn("Decline commands that are clearly wrong for the task", instructions)
        self.assertIn("Never type over the user's own draft in the captain pane", instructions)

    def test_agent_commands_work_outside_the_source_checkout(self):
        instructions = agents.agent_instructions(self.directory, "captain")
        command = next(line.strip() for line in instructions.splitlines() if " crew NAME " in line)
        argv = shlex.split(command)
        launcher = argv[: argv.index("crew")]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("HERDR_") and key != "PYTHONPATH"
        }
        help_result = subprocess.run(
            [*launcher, "--help"],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("create a native crew", help_result.stdout)
        guard_result = subprocess.run(
            launcher,
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(guard_result.returncode, 1)
        self.assertIn("Run captain inside a Herdr workspace", guard_result.stderr)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_launcher_renames_live_tab_and_executes_native_cli(self):
        for provider in ("claude", "codex"):
            with (
                self.subTest(provider=provider),
                patch.object(agents, "herdr") as api,
                patch.object(agents, "executable", return_value=f"/bin/{provider}"),
                patch.object(os, "execvpe") as execute,
                patch.object(sys.stdin, "isatty", return_value=True),
            ):
                args = self.args(
                    "--agent", provider, "--prompt", "literal `touch /tmp/no` $(false)"
                )
                agents.launch(args, self.pane, self.project)
                api.assert_called_once_with("tab", "rename", "w1:t1", "captain barbossa")
                binary, argv, env = execute.call_args.args
                self.assertEqual(binary, f"/bin/{provider}")
                self.assertEqual(argv[-1], args.prompt)
                self.assertEqual(env["CAPTAIN_SESSION"], self.meta["id"])
                self.assertEqual(env["CAPTAIN_PROJECT"], str(self.project))
                self.assertEqual(list(self.project.iterdir()), [])

    def test_captain_requires_an_agent_selection(self):
        for provider in ("claude", "codex"):
            with (
                self.subTest(provider=provider),
                patch.object(agents, "herdr"),
                patch.object(agents, "executable", side_effect=lambda name: f"/bin/{name}"),
                patch.object(os, "execvpe") as execute,
                patch.object(sys.stdin, "isatty", return_value=True),
                patch(
                    "captain_barbossa.prompts.questionary.select",
                    **{"return_value.unsafe_ask.return_value": provider},
                ) as ask,
            ):
                agents.launch(self.args(), self.pane, self.project)
                self.assertEqual(execute.call_args.args[0], f"/bin/{provider}")
                self.assertIn("Choose your captain", ask.call_args.args[0])
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch(
                "captain_barbossa.prompts.questionary.select",
                **{"return_value.unsafe_ask.return_value": None},
            ),
            patch.object(agents, "herdr") as api,
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "cancelled"):
                agents.launch(self.args(), self.pane, self.project)
            api.assert_not_called()

    def test_crew_requires_choices_and_cancellation_creates_nothing(self):
        for flags in ((), ("--agent", "codex"), ("--placement", "pane")):
            args = self.args("crew", "builder", "--task", "build", *flags)
            with (
                self.subTest(flags=flags),
                patch.object(sys.stdin, "isatty", return_value=False),
                patch.object(agents, "herdr") as api,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "Ask the user"):
                    agents.create_crew(args, self.pane, self.project)
                api.assert_not_called()
        for answers in ([None], ["claude", None]):
            with (
                patch.object(sys.stdin, "isatty", return_value=True),
                patch(
                    "captain_barbossa.prompts.questionary.select",
                    **{"return_value.unsafe_ask.side_effect": answers},
                ),
                patch.object(agents, "herdr") as api,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "cancelled"):
                    agents.create_crew(
                        self.args("crew", "builder", "--task", "build"), self.pane, self.project
                    )
                api.assert_not_called()

    def test_crew_creates_chosen_topology_then_starts_native_agent(self):
        for placement, provider in (("pane", "codex"), ("tab", "claude")):
            with self.subTest(placement=placement):
                task = 'Check quotes " and $() and `backticks`\nThen report.'
                args = self.args("crew", placement, "--task", task)
                created = {
                    "pane": {"pane_id": "w1:p2", "agent": provider, "agent_status": "idle"},
                    "root_pane": {"pane_id": "w1:p3"},
                    "agent": {"name": f"c-{self.meta['id'][:8]}-{placement}"},
                }
                with (
                    patch.object(agents, "herdr", return_value=created) as api,
                    patch.object(agents, "executable", return_value=f"/bin/{provider}"),
                    patch.object(sys.stdin, "isatty", return_value=True),
                    patch(
                        "captain_barbossa.prompts.questionary.select",
                        **{"return_value.unsafe_ask.side_effect": [provider, placement]},
                    ) as ask,
                ):
                    agents.create_crew(args, self.pane, self.project)
                calls = api.call_args_list
                self.assertEqual(
                    calls[0].args[:2],
                    ("pane", "split") if placement == "pane" else ("tab", "create"),
                )
                self.assertIn(f"CAPTAIN_SESSION={self.meta['id']}", calls[0].args)
                self.assertIn("Choose your crew agent", ask.call_args_list[0].args[0])
                self.assertIn("Where should the crew open?", ask.call_args_list[-1].args[0])
                run = next(call for call in calls if call.args[:2] == ("pane", "run"))
                self.assertEqual(run.args[-1], '/bin/sh "$CAPTAIN_CREW_LAUNCHER"')
                self.assertEqual(run.kwargs, {"expect_output": False})
                self.assertEqual(calls[-3].args[:2], ("agent", "rename"))
                self.assertEqual(calls[-2].args, ("agent", "get", run.args[2]))
                self.assertEqual(
                    calls[-1].args,
                    ("agent", "prompt", f"c-{self.meta['id'][:8]}-{placement}", task),
                )
                saved = memory.read_json(self.directory / "session.json")["crew"][placement]
                self.assertEqual(saved["status"], "started")
                self.assertEqual(saved["provider"], provider)
                launcher = self.directory / f"crew-{placement}.sh"
                self.assertIn(f"CAPTAIN_CREW_LAUNCHER={launcher}", calls[0].args)
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(self.project.iterdir()), [])

    def test_long_native_instructions_survive_canonical_terminal_input(self):
        native = self.root / "fake agent's cli"
        received = self.root / "received.json"
        capture = "import json, os, sys; from pathlib import Path; Path(os.environ['CAPTAIN_TEST_ARGS']).write_text(json.dumps(sys.argv[1:]))"
        native.write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", capture]) + ' "$@"\n'
        )
        native.chmod(0o700)
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                instructions = (
                    "Long instructions: " + "quotes ' \" `false` $(false) \\ and newlines\n" * 100
                )
                args = self.args(
                    "crew", provider, "--agent", provider, "--task", "check", "--placement", "pane"
                )
                created = {"pane": {"pane_id": "w1:p2", "agent": provider, "agent_status": "idle"}}
                created["agent"] = {"name": f"c-{self.meta['id'][:8]}-{provider}"}
                with (
                    patch.object(agents, "herdr", return_value=created) as api,
                    patch.object(agents, "executable", return_value=str(native)),
                    patch.object(agents, "agent_instructions", return_value=instructions),
                ):
                    agents.create_crew(args, self.pane, self.project)
                command = next(
                    call.args[-1] for call in api.call_args_list if call.args[:2] == ("pane", "run")
                )
                env = dict(
                    os.environ,
                    CAPTAIN_CREW_LAUNCHER=str(self.directory / f"crew-{provider}.sh"),
                    CAPTAIN_TEST_ARGS=str(received),
                )
                master, slave = pty.openpty()
                try:
                    self.assertLess(len(command.encode()), os.fpathconf(slave, "PC_MAX_CANON"))
                    # read waits in canonical mode, as a newly opened terminal can do.
                    with subprocess.Popen(
                        ["/bin/sh", "-c", 'IFS= read -r command; eval "$command"'],
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        env=env,
                    ) as process:
                        try:
                            os.write(master, (command + "\n").encode())
                            self.assertEqual(process.wait(timeout=10), 0)
                        finally:
                            if process.poll() is None:
                                process.kill()
                    self.assertEqual(
                        json.loads(received.read_text()), agents.native_args(provider, instructions)
                    )
                finally:
                    os.close(master)
                    os.close(slave)

    def test_startup_failure_preserves_pane_and_prevents_duplicate_retry(self):
        args = self.args(
            "crew", "broken", "--agent", "codex", "--task", "build", "--placement", "pane"
        )

        def api(*args, **kwargs):
            if args[:2] == ("pane", "run"):
                raise runtime.CaptainError("agent_not_ready")
            return {"pane": {"pane_id": "w1:p2"}}

        with (
            patch.object(agents, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/codex"),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "pane was preserved"):
                agents.create_crew(args, self.pane, self.project)
            self.assertFalse(
                any(call.args[:2] == ("pane", "close") for call in calls.call_args_list)
            )
            self.assertFalse(
                any(call.args[:2] == ("agent", "prompt") for call in calls.call_args_list)
            )
            with self.assertRaisesRegex(runtime.CaptainError, "already exists"):
                agents.create_crew(args, self.pane, self.project)
        saved = memory.read_json(self.directory / "session.json")
        self.assertEqual(saved["crew"]["broken"]["status"], "needs_attention")

    def test_startup_waits_for_the_expected_native_agent(self):
        states = [
            {},
            {"agent": "claude", "agent_status": "idle"},
            {"agent": "codex", "agent_status": "working"},
            {"agent": "codex", "agent_status": "done"},
        ]

        def api(*args, **kwargs):
            return (
                {"pane": states.pop(0)}
                if args[:2] == ("pane", "get")
                else {"agent": {"name": "builder"}}
            )

        with (
            patch.object(agents, "herdr", side_effect=api) as calls,
            patch.object(agents.time, "sleep"),
        ):
            agents.wait_for_crew("w1:p2", "codex", "builder")
        self.assertEqual(states, [])
        self.assertEqual(calls.call_args_list[-2].args, ("agent", "rename", "w1:p2", "builder"))
        self.assertEqual(calls.call_args_list[-1].args, ("agent", "get", "w1:p2"))

    def test_startup_rejects_unverified_agent_name(self):
        for response in ({"agent": {"name": "other"}}, {"agent": {}}, {}):
            with (
                self.subTest(response=response),
                patch.object(
                    agents,
                    "herdr",
                    side_effect=[
                        {"pane": {"agent": "codex", "agent_status": "idle"}},
                        {},
                        response,
                    ],
                ) as api,
                patch.object(agents.time, "sleep") as sleep,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "rename failed for pane w1:p2"):
                    agents.wait_for_crew("w1:p2", "codex", "builder")
                self.assertEqual(
                    [call.args for call in api.call_args_list[-2:]],
                    [("agent", "rename", "w1:p2", "builder"), ("agent", "get", "w1:p2")],
                )
                sleep.assert_not_called()

    def test_blocked_or_timed_out_startup_never_submits_the_task(self):
        for status in ("blocked", "unknown"):
            with self.subTest(status=status):
                args = self.args(
                    "crew", status, "--agent", "codex", "--task", "build", "--placement", "pane"
                )
                created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": status}}
                created["agent"] = {"name": f"c-{self.meta['id'][:8]}-{status}"}
                with (
                    patch.object(agents, "herdr", return_value=created) as calls,
                    patch.object(agents, "executable", return_value="/bin/codex"),
                    patch.object(agents.time, "monotonic", side_effect=[0, 1, 31]),
                    patch.object(agents.time, "sleep"),
                ):
                    message = "input or approval" if status == "blocked" else "did not become ready"
                    with self.assertRaisesRegex(runtime.CaptainError, message):
                        agents.create_crew(args, self.pane, self.project)
                self.assertFalse(
                    any(call.args[:2] == ("agent", "prompt") for call in calls.call_args_list)
                )
                self.assertFalse(
                    any(call.args[:2] == ("pane", "close") for call in calls.call_args_list)
                )
                saved = memory.read_json(self.directory / "session.json")["crew"][status]
                self.assertEqual(saved["status"], "needs_attention")

    def test_graph_scopes_projects_sessions_and_parallel_relationships(self):
        shared = self.directory.parent.parent / "graph.json"
        memory.add_memory(shared, "project", "uses", "Python")
        memory.add_memory(self.directory / "graph.json", "task", "uses", "secret-session-fact")
        memory.add_memory(shared, "project", "tests_with", "Python")
        other, _ = memory.session(self.project, self.pane, create=True)
        snapshot = memory.memory_snapshot(other)
        graph = memory.read_json(snapshot / "graph.json")
        self.assertEqual(len(graph["links"]), 2)
        self.assertNotIn("secret-session-fact", json.dumps(graph))
        project2 = self.root / "other-project"
        project2.mkdir()
        isolated, _ = memory.session(project2, self.pane, create=True)
        self.assertEqual(
            memory.read_json(memory.memory_snapshot(isolated) / "graph.json")["nodes"], []
        )
        with self.assertRaisesRegex(runtime.CaptainError, "another project or Herdr workspace"):
            memory.session(self.project, dict(self.pane, workspace_id="w2"), self.meta["id"])

    def test_graph_concurrent_writes_and_corruption_preservation(self):
        graph_path = self.directory / "graph.json"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(lambda i: memory.add_memory(graph_path, "task", "has", str(i)), range(20))
            )
        self.assertEqual(len(memory.read_json(graph_path)["links"]), 20)
        graph_path.write_text("broken json")
        with self.assertRaisesRegex(runtime.CaptainError, "Cannot read memory"):
            memory.add_memory(graph_path, "x", "y", "z")
        self.assertEqual(graph_path.read_text(), "broken json")

    def test_memory_rejects_repo_storage_and_invalid_session_paths(self):
        with patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.project / ".memory")}):
            with self.assertRaisesRegex(runtime.CaptainError, "outside the project"):
                memory.storage(self.project)
        with self.assertRaisesRegex(runtime.CaptainError, "Invalid captain session"):
            memory.session(self.project, self.pane, "../../elsewhere")
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(shutil.which("graphify"), "Graphify is optional")
    def test_real_graphify_reads_memory_without_writing_in_project(self):
        memory.add_memory(self.directory / "graph.json", "rate limiter", "uses", "per-user windows")
        snapshot = memory.memory_snapshot(self.directory)
        env = dict(os.environ, GRAPHIFY_OUT=str(snapshot), GRAPHIFY_QUERY_LOG_DISABLE="1")
        result = subprocess.run(
            [
                runtime.executable("graphify"),
                "query",
                "rate limiter",
                "--graph",
                str(snapshot / "graph.json"),
            ],
            cwd=snapshot,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("per-user windows", result.stdout)
        self.assertEqual(list(self.project.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
