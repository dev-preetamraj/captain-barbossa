import contextlib
import io
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase


class StatusCrewTests(SessionCase):
    """Regression tests for `captain status` (agents.status_crew)."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
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
        store.write_json(self.directory / "session.json", self.meta)

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
        store.write_json(self.directory / "session.json", self.meta)
        output = self.status(lambda *a, **k: (_ for _ in ()).throw(AssertionError(a)))
        self.assertEqual(output.strip(), "No crew.")


class FocusTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # nudge_block/nudge read a realistic agent status these ad hoc herdr fakes don't model;
        # a mail doorbell is not what these tests exercise, so give delivery a clean ring by
        # default. test_submit.py covers nudge_block/nudge themselves against real fakes.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_focus_command_resolves_names_and_ids_without_sending_input(self):
        self.meta["crew"] = {
            "jack": {
                "name": "Jack",
                "agent": "c-session-jack",
                "pane": "w1:p2",
                "placement": "pane",
                "status": "needs_attention",
            },
            "will": {
                "name": "Will",
                "agent": "c-session-will",
                "pane": "w1:p3",
                "tab": "w1:t2",
                "placement": "tab",
                "status": "started",
            },
            "jack-2": {"name": "Jack-2", "agent": "c-session-jack-2", "pane": "w1:p4"},
            "scout": {"agent": "c-session-scout", "pane": "w1:p5", "tab": "w1:t1"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        live = {"c-session-jack-2": {"agent": {"tab_id": "w1:t3"}}}
        for name, crew_id, tab in (
            ("Jack", "jack", None),
            (" jAcK ", "jack", None),
            ("jack", "jack", None),
            ("c-session-jack", "jack", None),
            ("will", "will", "w1:t2"),
            ("Jack-2", "jack-2", "w1:t3"),
            ("SCOUT", "scout", None),
        ):
            crew = self.meta["crew"][crew_id]
            with (
                self.subTest(name=name),
                patch.object(cli, "current_pane", return_value=self.pane),
                patch.object(cli, "project_root", return_value=self.project),
                patch.object(
                    runtime, "herdr", side_effect=lambda *call, **_: live.get(call[2], {})
                ) as api,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(["--session", self.meta["id"], "focus", name]), 0)
                expected = [("agent", "get", crew["agent"])]
                if tab:
                    expected.append(("tab", "focus", tab))
                expected.append(("agent", "focus", crew["agent"]))
                self.assertEqual([call.args for call in api.call_args_list], expected)
                self.assertIn("Focused", output.getvalue())
        self.assertEqual(store.read_json(self.directory / "session.json"), self.meta)
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(
                runtime, "herdr", side_effect=runtime.CaptainError("agent_not_found")
            ) as api,
            contextlib.redirect_stderr(io.StringIO()) as error,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.meta["id"], "focus", "Jack"]), 1)
            self.assertIn("Could not focus Jack", error.getvalue())
            self.assertEqual(output.getvalue(), "")
            api.assert_called_once_with("agent", "get", "c-session-jack")

    def test_focus_rejects_unknown_and_ambiguous_names_without_leaving_the_session(self):
        self.meta["crew"] = {
            "jack": {"name": "Jack", "agent": "c-session-jack"},
            "legacy-jack": {"name": "Jack", "agent": "c-session-legacy-jack"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        other, meta = sessions.session(self.project, self.pane, create=True)
        meta["crew"] = {"elizabeth": {"name": "Elizabeth", "agent": "c-other-elizabeth"}}
        store.write_json(other / "session.json", meta)
        with patch.object(runtime, "herdr") as api:
            for name, message in (
                ("Elizabeth", "Available crew"),
                ("", "No crew"),
                ("Jack", "ambiguous"),
            ):
                with self.subTest(name=name), self.assertRaisesRegex(runtime.CaptainError, message):
                    agents.focus_crew(self.args("focus", name), self.pane, self.project)
                api.assert_not_called()
            args = self.args("focus", "Jack")
            args.session = None
            with self.assertRaisesRegex(runtime.CaptainError, "Start captain first"):
                agents.focus_crew(args, self.pane, self.project)
            api.assert_not_called()
            api.return_value = {}
            agents.focus_crew(self.args("focus", "c-session-legacy-jack"), self.pane, self.project)
            self.assertEqual(
                [call.args for call in api.call_args_list],
                [
                    ("agent", "get", "c-session-legacy-jack"),
                    ("agent", "focus", "c-session-legacy-jack"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
