import contextlib
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, runtime
from captain_barbossa import instructions as instruction_prompts
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew


class WaitCrewMemoryTests(unittest.TestCase):
    """Regression tests for wait_crew's graph memory writes (agents.wait_crew, ~L381)."""

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
        agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": agent_name,
                "provider": "claude",
                "task": "build",
                "pane": "w1:p2",
                "tab": "w1:t1",
                "status": "started",
            }
        }
        memory.write_json(self.directory / "session.json", self.meta)
        self.agent_name = agent_name

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def wait_api(self, statuses, tail="", report=None):
        remaining = list(statuses)
        status = "working"

        def api(*args, **kwargs):
            nonlocal status
            if args[:2] == ("agent", "read"):
                status = remaining.pop(0) if remaining else status
                if report and status in ("idle", "blocked"):
                    memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                return tail + ("\n❯" if status == "idle" else "\nworking")
            raise AssertionError(f"unexpected herdr call: {args}")

        return api

    def run_wait(self, statuses, tail="", report=None):
        with (
            patch.object(runtime, "herdr", side_effect=self.wait_api(statuses, tail, report)),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "60"), self.pane, self.project)
        return output.getvalue()

    def edges(self, relation):
        path = self.directory / "graph.json"
        if not path.exists():
            return []
        graph = memory.read_json(path)
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        return [
            labels[link["target"]]
            for link in graph["links"]
            if link["relation"] == relation and labels[link["source"]] == "Jack"
        ]

    def test_completion_with_a_report_omits_the_pane_tail(self):
        printed = self.run_wait(["idle", "idle", "idle"], report="tests pass")
        self.assertIn("idle; reported: tests pass", printed)
        self.assertEqual(self.edges("tail"), [])
        self.assertNotIn("pane tail", printed)

    def test_completion_with_a_report_does_not_duplicate_the_report_text(self):
        self.run_wait(["idle", "idle", "idle"], report="tests pass")
        self.assertEqual(self.edges("completed"), ["idle; reported"])

    def test_completion_writes_exactly_once(self):
        self.run_wait(["idle", "idle", "idle"], report="tests pass")
        self.assertEqual(len(self.edges("completed")), 1)

    def test_no_report_records_a_short_tail_edge_but_prints_the_full_tail(self):
        long_tail = "x" * 500
        printed = self.run_wait(["idle", "idle", "idle"], tail=long_tail)
        self.assertEqual(self.edges("completed"), ["idle; no report recorded"])
        tail_edges = self.edges("tail")
        self.assertEqual(len(tail_edges), 1)
        self.assertLessEqual(len(tail_edges[0]), 300)
        self.assertIn(f"pane tail: {long_tail}", printed)

    def run_pi_wait(self, tail, report=None):
        """A pi wait: pi installs no hooks, so it always reaches the pane-tail fallback."""
        self.meta["crew"]["jack"]["provider"] = "pi"
        memory.write_json(self.directory / "session.json", self.meta)

        def api(*args, **kwargs):
            if args[:2] == ("agent", "get"):
                if report:
                    memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                return {"agent": {"agent_status": "idle"}}
            if args[:2] == ("agent", "read"):
                return tail
            raise AssertionError(f"unexpected herdr call: {args}")

        with (
            patch.object(runtime, "herdr", side_effect=api),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "60"), self.pane, self.project)
        return output.getvalue()

    def test_pi_wait_files_the_pane_tail_instead_of_printing_it(self):
        tail = "\n".join(f"line {index} of noisy pi terminal chrome" for index in range(60))
        printed = self.run_pi_wait(tail)
        self.assertNotIn("line 59 of noisy pi terminal chrome", printed)
        filed = self.directory / "tail-jack.txt"
        self.assertIn(str(filed), printed)
        self.assertIn("line 59 of noisy pi terminal chrome", filed.read_text(encoding="utf-8"))

    def test_pi_wait_delivery_stays_a_couple_of_short_lines(self):
        """The captain extension steers whatever wait prints into pi, so size is context cost."""
        printed = self.run_pi_wait("x" * 1400)
        self.assertLess(len(printed), 400)
        self.assertEqual(len(printed.splitlines()), 2)
        self.assertIn("idle; no report recorded", printed)

    def test_pi_wait_with_a_report_prints_the_report_and_files_no_tail(self):
        printed = self.run_pi_wait("y" * 1400, report="tests pass")
        self.assertIn("idle; reported: tests pass", printed)
        self.assertNotIn("pane tail", printed)
        self.assertFalse((self.directory / "tail-jack.txt").exists())

    def test_non_pi_crew_still_print_the_tail_inline(self):
        printed = self.run_wait(["idle", "idle", "idle"], tail="ran 66 tests\nOK")
        self.assertIn("pane tail: ran 66 tests\nOK", printed)
        self.assertFalse((self.directory / "tail-jack.txt").exists())

    def event_path(self):
        return memory.private_dir(self.directory / "events") / "jack.jsonl"

    def write_event(self, event):
        with self.event_path().open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")

    def test_native_hook_commands_append_one_json_line_from_stdin_or_argv(self):
        events = self.root / "events with 'quotes' $() and spaces.jsonl"
        expected = []
        for provider in ("claude", "codex"):
            args = instruction_prompts.native_args(provider, "instructions", events=events)
            if provider == "claude":
                settings = json.loads(args[args.index("--settings") + 1])
                self.assertEqual(
                    settings["attribution"],
                    json.loads(instruction_prompts.CLAUDE_NO_ATTRIBUTION)["attribution"],
                )
                self.assertEqual(
                    set(settings["hooks"]),
                    {"SessionStart", "Stop", "Notification", "PermissionRequest"},
                )
                for kind, entries in settings["hooks"].items():
                    event = {"hook_event_name": kind, "message": "quotes '\"\n雪"}
                    command = entries[0]["hooks"][0]["command"]
                    result = subprocess.run(
                        ["/bin/sh", "-c", command],
                        input=json.dumps(event, indent=2),
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    expected.append(event)
            else:
                command = tomllib.loads(next(arg for arg in args if arg.startswith("notify=")))[
                    "notify"
                ]
                event = {"type": "agent-turn-complete", "last-assistant-message": "done\nOK"}
                result = subprocess.run(
                    [*command, json.dumps(event)], text=True, capture_output=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected.append(event)
        self.assertEqual([json.loads(line) for line in events.read_text().splitlines()], expected)

    def test_wait_consumes_native_completion_before_wait_without_reading_herdr(self):
        for event, status in (
            ({"hook_event_name": "Stop"}, "done"),
            (
                {
                    "type": "agent-turn-complete",
                    "input-messages": ["build"],
                    "last-assistant-message": "checks pass",
                },
                "done",
            ),
            ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, "idle"),
            (
                {"hook_event_name": "Notification", "notification_type": "permission_prompt"},
                "blocked",
            ),
            ({"hook_event_name": "PermissionRequest"}, "blocked"),
        ):
            with self.subTest(event=event), patch.object(runtime, "herdr") as api:
                self.write_event(event)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    agents.wait_crew(
                        self.args("wait", "Jack", "--timeout", "0"), self.pane, self.project
                    )
                self.assertIn(f"Jack {status}.", output.getvalue())
                self.assertNotIn("pane tail", output.getvalue())
                if "last-assistant-message" in event:
                    self.assertIn("checks pass", output.getvalue())
                api.assert_not_called()
                self.assertEqual(
                    Crew(
                        "jack",
                        {"agent": self.agent_name},
                        memory.Session(self.directory, self.meta),
                    ).status(0),
                    (None, None),
                )

    def test_wait_ignores_codex_title_completion_until_the_submitted_task_finishes(self):
        self.write_event(
            {
                "type": "agent-turn-complete",
                "input-messages": ["Generate a concise, single-line task title..."],
                "last-assistant-message": '{"title":"Build the project"}',
            }
        )
        completion = {
            "type": "agent-turn-complete",
            "input-messages": ["build"],
            "last-assistant-message": "Real task finished",
        }
        with patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(agents.CaptainError, "still working"):
                agents.wait_crew(
                    self.args("wait", "Jack", "--timeout", "0"), self.pane, self.project
                )
            self.assertEqual(self.edges("completed"), [])
            with (
                patch.object(
                    panes.time, "sleep", side_effect=lambda _: self.write_event(completion)
                ) as sleep,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                agents.wait_crew(
                    self.args("wait", "Jack", "--timeout", "60"), self.pane, self.project
                )
            sleep.assert_called_once()
            self.assertIn("done; no report recorded; hook: Real task finished", output.getvalue())
            self.assertNotIn("Build the project", output.getvalue())
            api.assert_not_called()

    def test_wait_prints_claude_stop_and_permission_details(self):
        for event, detail in (
            ({"hook_event_name": "Stop", "last_assistant_message": "hello"}, "hello"),
            (
                {
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pwd"},
                },
                'Bash {"command": "pwd"}',
            ),
        ):
            for report in (
                (None, "partial") if event["hook_event_name"] == "PermissionRequest" else (None,)
            ):
                with self.subTest(event=event, report=report):
                    if report:
                        memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                    self.write_event(event)
                    with (
                        patch.object(runtime, "herdr") as api,
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        agents.wait_crew(
                            self.args("wait", "Jack", "--timeout", "0"), self.pane, self.project
                        )
                    self.assertIn(f"hook: {detail}", output.getvalue())
                    if report:
                        self.assertIn("reported: partial", output.getvalue())
                    api.assert_not_called()

    def test_wait_uses_report_written_before_an_unconsumed_stop(self):
        memory.add_memory(self.directory / "graph.json", "Jack", "report", "tests pass")
        self.write_event({"hook_event_name": "Stop"})
        with (
            patch.object(runtime, "herdr") as api,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "0"), self.pane, self.project)
        self.assertIn("done; reported: tests pass", output.getvalue())
        api.assert_not_called()
        self.write_event({"hook_event_name": "PermissionRequest"})
        with (
            patch.object(runtime, "herdr") as api,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack", "--timeout", "0"), self.pane, self.project)
        self.assertIn("blocked; no report recorded", output.getvalue())
        self.assertNotIn("tests pass", output.getvalue())
        api.assert_not_called()

    def test_wait_tails_events_arriving_after_start_and_prefers_latest_state(self):
        self.write_event({"hook_event_name": "PermissionRequest"})
        self.write_event({"hook_event_name": "SessionStart"})
        stop = {"hook_event_name": "Stop"}
        with (
            patch.object(runtime, "herdr") as api,
            patch.object(panes.time, "sleep", side_effect=lambda _: self.write_event(stop)),
        ):
            self.assertEqual(
                Crew(
                    "jack", {"agent": self.agent_name}, memory.Session(self.directory, self.meta)
                ).status(10),
                ("done", stop),
            )
        api.assert_not_called()

    def test_session_start_does_not_finish_wait_or_trigger_pane_fallback(self):
        self.write_event({"hook_event_name": "SessionStart"})
        with (
            patch.object(runtime, "herdr") as api,
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            patch.object(panes.time, "sleep"),
        ):
            self.assertEqual(
                Crew(
                    "jack", {"agent": self.agent_name}, memory.Session(self.directory, self.meta)
                ).status(20),
                (None, None),
            )
        api.assert_not_called()

    def pi_crew(self):
        # pi's composer has no prompt glyph, so the pane fallback can never see it finish.
        return Crew(
            "jack",
            {"agent": self.agent_name, "provider": "pi"},
            memory.Session(self.directory, self.meta),
        )

    def pi_status(self, crew, statuses, timeout=60):
        def api(*args, **kwargs):
            self.assertEqual(args[:2], ("agent", "get"))
            return {"agent": {"agent_status": statuses.pop(0)}}

        with (
            patch.object(runtime, "herdr", side_effect=api),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            return crew.status(timeout)

    def test_pi_wait_finishes_a_task_that_was_already_idle_at_the_first_poll(self):
        # A task shorter than the grace period is never once seen working.
        statuses = ["idle"] * 3
        self.assertEqual(self.pi_status(self.pi_crew(), statuses), ("idle", None))
        self.assertEqual(statuses, [])

    def test_pi_wait_needs_consecutive_idle_reads(self):
        statuses = ["idle", "working", "idle", "idle", "idle"]
        self.assertEqual(self.pi_status(self.pi_crew(), statuses), ("idle", None))
        self.assertEqual(statuses, [])

    def test_pi_wait_reads_no_herdr_status_within_the_grace_period(self):
        with patch.object(runtime, "herdr") as unused:
            self.assertEqual(self.pi_crew().status(0), (None, None))
        unused.assert_not_called()

    def test_pi_wait_reports_herdr_done_without_waiting_for_idle(self):
        crew = Crew(
            "jack",
            {"agent": self.agent_name, "provider": "pi"},
            memory.Session(self.directory, self.meta),
        )
        with (
            patch.object(runtime, "herdr", return_value={"agent": {"agent_status": "done"}}),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            self.assertEqual(crew.status(60), ("done", None))

    def test_event_reader_skips_bad_records_and_retries_a_partial_unicode_line(self):
        events = self.event_path()
        self.assertEqual(memory.read_events(events, 0), ([], 0))
        prefix = b"invalid\n[]\nnull\n\xff\n"
        event = {"hook_event_name": "Stop", "message": "雪"}
        encoded = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        cut = encoded.index("雪".encode()) + 1
        events.write_bytes(prefix + encoded[:cut])
        self.assertEqual(memory.read_events(events, 0), ([], len(prefix)))
        with events.open("ab") as file:
            file.write(encoded[cut:])
        self.assertEqual(memory.read_events(events, len(prefix)), ([event], events.stat().st_size))


if __name__ == "__main__":
    unittest.main()
