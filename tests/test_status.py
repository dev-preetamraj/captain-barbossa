import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, runtime


class StatusCrewTests(unittest.TestCase):
    """Regression tests for `captain status` (agents.status_crew)."""

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
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": "c-abc-jack",
                "provider": "claude",
                "model": "claude-sonnet-5",
                "pane": "w1:p2",
                "task": "build the thing\nmore detail here",
                "status": "started",
            },
            "will": {
                "id": "will",
                "name": "Will",
                "agent": "c-abc-will",
                "provider": "codex",
                "task": "x" * 80,
                "status": "started",
            },
            "gibbs": {
                "id": "gibbs",
                "name": "Gibbs",
                "agent": "c-abc-gibbs",
                "provider": "claude",
                "task": "retired",
                "status": "dismissed",
            },
        }
        memory.write_json(self.directory / "session.json", self.meta)

    def args(self, *extra):
        return cli.parser().parse_args(["--session", self.meta["id"], "status", *extra])

    def status(self, api, *extra):
        with (
            patch.object(runtime, "herdr", side_effect=api),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.status_crew(self.args(*extra), self.pane, self.project)
        return output.getvalue()

    def test_table_refreshes_status_and_skips_dismissed(self):
        def api(*call_args, **kwargs):
            if call_args[:2] == ("agent", "get"):
                name = call_args[2]
                return {"agent": {"agent_status": "idle" if name == "c-abc-jack" else "working"}}
            raise AssertionError(f"unexpected herdr call: {call_args}")

        output = self.status(api)
        lines = output.splitlines()
        self.assertIn("NAME", lines[0])
        jack_line = next(line for line in lines if line.startswith("Jack"))
        self.assertIn("idle", jack_line)
        self.assertIn("build the thing", jack_line)
        self.assertNotIn("Gibbs", output)
        will_line = next(line for line in lines if line.startswith("Will"))
        self.assertIn("working", will_line)
        self.assertLessEqual(len(will_line.split("  ")[-1]), 60)

    def test_all_flag_includes_dismissed_crew(self):
        def api(*call_args, **kwargs):
            if call_args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "idle"}}
            raise AssertionError(f"unexpected herdr call: {call_args}")

        output = self.status(api, "--all")
        self.assertIn("Gibbs", output)

    def test_herdr_error_keeps_recorded_status(self):
        def api(*call_args, **kwargs):
            if call_args[:2] == ("agent", "get"):
                raise agents.CaptainError("herdr unreachable")
            raise AssertionError(f"unexpected herdr call: {call_args}")

        output = self.status(api)
        jack_line = next(line for line in output.splitlines() if line.startswith("Jack"))
        self.assertIn("started", jack_line)

    def test_no_crew_prints_placeholder(self):
        self.meta["crew"] = {}
        memory.write_json(self.directory / "session.json", self.meta)
        output = self.status(lambda *a, **k: (_ for _ in ()).throw(AssertionError(a)))
        self.assertEqual(output.strip(), "No crew.")


if __name__ == "__main__":
    unittest.main()
