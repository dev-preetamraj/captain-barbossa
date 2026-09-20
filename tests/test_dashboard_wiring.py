import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, memory, runtime
from captain_barbossa.memory import Session
from captain_barbossa.pane import Pane
from captain_barbossa.placement import Placement
from captain_barbossa.runtime import CaptainError

PANE = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}


class LaunchHarness(unittest.TestCase):
    """A captain launch with Herdr, the native CLI and the clock all stubbed out."""

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
        os.environ.pop("CAPTAIN_SESSION", None)
        self.calls = []
        self.splits = 0
        self.enterContext(patch.object(runtime, "herdr", self._herdr))
        self.enterContext(patch.object(agents, "check_for_update", lambda: None))
        self.enterContext(patch.object(agents.instructions, "agent_instructions", lambda *a: "x"))
        self.enterContext(patch.object(agents.sys.stdin, "isatty", lambda: True))
        self.execs = []
        self.enterContext(
            patch.object(agents.os, "execvpe", lambda b, c, e: self.execs.append((b, c, e)))
        )
        self.enterContext(patch.object(agents, "executable", lambda name: f"/bin/{name}"))
        # This harness exists to test the dashboard pane, so opt in to [dashboard] enabled.
        self.enterContext(patch.object(agents.config, "flag", lambda *names: True))

    def _herdr(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("pane", "split"):
            # The dashboard splits first, so it is always w1:p2; crew take later ids.
            self.splits += 1
            return {"pane": {"pane_id": f"w1:p{self.splits + 1}", "tab_id": "w1:t1"}}
        if args[:2] == ("tab", "create"):
            return {"root_pane": {"pane_id": "w1:p9", "tab_id": "w1:t2"}, "tab_id": "w1:t2"}
        return {}

    def launch(self, argv=()):
        args = cli.parser().parse_args(list(argv))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            agents.launch(args, PANE, self.project)
        return stderr.getvalue()

    def split_call(self):
        return next((c for c in self.calls if c[:2] == ("pane", "split")), None)

    def session_dir(self):
        _, _, env = self.execs[0]
        return memory.session(self.project, PANE, env["CAPTAIN_SESSION"]).directory


class DashboardWiringTests(LaunchHarness):
    def test_launch_splits_a_dashboard_pane_and_still_execs(self):
        self.launch(["--agent", "claude"])
        split = self.split_call()
        self.assertIsNotNone(split)
        self.assertEqual(split[split.index("--pane") + 1], "w1:p1")
        self.assertEqual(split[split.index("--direction") + 1], "down")
        self.assertIn("--no-focus", split)
        self.assertEqual([binary for binary, _, _ in self.execs], ["/bin/claude"])

    def test_dashboard_pane_is_short_not_half_the_screen(self):
        self.launch(["--agent", "claude"])
        split = self.split_call()
        ratio = float(split[split.index("--ratio") + 1])
        # Ratio is what the captain keeps, so the dashboard gets the small remainder.
        self.assertGreaterEqual(ratio, 0.7)
        self.assertLess(ratio, 1.0)

    def test_dashboard_pane_runs_captain_dashboard(self):
        self.launch(["--agent", "claude"])
        run = next(c for c in self.calls if c[:2] == ("pane", "run"))
        self.assertEqual(run[2], "w1:p2")
        launcher = next(
            arg.split("=", 1)[1]
            for arg in self.split_call()
            if arg.startswith("CAPTAIN_DASHBOARD_LAUNCHER=")
        )
        # The path travels as an env var so a quote in it cannot inject shell.
        self.assertEqual(run[3], '/bin/sh "$CAPTAIN_DASHBOARD_LAUNCHER"')
        script = Path(launcher).read_text(encoding="utf-8")
        self.assertIn("-m captain_barbossa", script)
        self.assertIn("dashboard", script.split())
        self.assertIn("--session", script.split())

    def test_no_dashboard_skips_the_split(self):
        self.launch(["--agent", "claude", "--no-dashboard"])
        self.assertIsNone(self.split_call())
        self.assertEqual(len(self.execs), 1)

    def test_split_failure_warns_and_still_execs(self):
        def failing(*args, **kwargs):
            self.calls.append(args)
            if args[:2] == ("pane", "split"):
                raise CaptainError("pane is gone")
            return {}

        with patch.object(runtime, "herdr", failing):
            stderr = self.launch(["--agent", "claude"])
        self.assertIn("no dashboard pane", stderr)
        self.assertIn("pane is gone", stderr)
        self.assertEqual(len(self.execs), 1)

    def test_missing_pane_id_warns_and_still_execs(self):
        def empty(*args, **kwargs):
            self.calls.append(args)
            return {}

        with patch.object(runtime, "herdr", empty):
            stderr = self.launch(["--agent", "claude"])
        self.assertIn("no dashboard pane", stderr)
        self.assertEqual(len(self.execs), 1)

    def test_captain_hooks_append_to_its_own_event_file(self):
        self.launch(["--agent", "claude"])
        _, argv, _ = self.execs[0]
        settings = json.loads(argv[argv.index("--settings") + 1])
        events = self.session_dir() / "events" / "captain.jsonl"
        self.assertEqual(
            set(settings["hooks"]), {"SessionStart", "Stop", "Notification", "PermissionRequest"}
        )
        self.assertIn(str(events), json.dumps(settings["hooks"]))
        # append_event creates the file itself, but never its parent directory.
        self.assertTrue(events.parent.is_dir())

    def test_captain_is_not_added_to_the_crew_roster(self):
        self.launch(["--agent", "claude"])
        _, _, env = self.execs[0]
        meta = memory.session(self.project, PANE, env["CAPTAIN_SESSION"]).meta
        self.assertEqual(meta["crew"], {})

    def test_captain_json_records_the_dashboard_pane(self):
        self.launch(["--agent", "claude"])
        record = json.loads((self.session_dir() / "captain.json").read_text(encoding="utf-8"))
        self.assertEqual(record["dashboard"], "w1:p2")
        self.assertEqual(record["pane"], "w1:p1")

    def test_context_limit_forwarded_when_set(self):
        with patch.dict(os.environ, {"CAPTAIN_CONTEXT_LIMIT": "42000"}):
            self.launch(["--agent", "claude"])
        self.assertIn("CAPTAIN_CONTEXT_LIMIT=42000", self.split_call())

    def test_context_limit_omitted_when_unset(self):
        os.environ.pop("CAPTAIN_CONTEXT_LIMIT", None)
        self.launch(["--agent", "claude"])
        self.assertFalse(any(arg.startswith("CAPTAIN_CONTEXT_LIMIT=") for arg in self.split_call()))

    def test_no_dashboard_records_no_pane(self):
        self.launch(["--agent", "claude", "--no-dashboard"])
        record = json.loads((self.session_dir() / "captain.json").read_text(encoding="utf-8"))
        self.assertIsNone(record["dashboard"])


class DashboardCommandTests(unittest.TestCase):
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
        os.environ.pop("CAPTAIN_SESSION", None)
        self.current = memory.session(self.project, PANE, create=True)
        self.session_id = self.current.meta["id"]

    def run_dashboard(self, argv):
        seen = []
        args = cli.parser().parse_args(["--session", self.session_id, *argv])
        with patch.object(agents.dashboard, "run", lambda *call: seen.append(call)):
            agents.run_dashboard(args, PANE, self.project)
        return seen[0]

    def test_resolves_the_session_like_status_does(self):
        (current,) = self.run_dashboard(["dashboard"])
        self.assertTrue(current.directory.exists())
        self.assertIn("id", current.meta)

    def test_interval_is_forwarded_when_given(self):
        self.assertEqual(self.run_dashboard(["dashboard", "--interval", "5"])[1], 5.0)

    def test_only_interval_is_added(self):
        board = cli.parser()._subparsers._group_actions[0].choices["dashboard"]
        flags = {option for action in board._actions for option in action.option_strings}
        self.assertEqual(flags - {"-h", "--help"}, {"--interval"})


class RenestTests(unittest.TestCase):
    """`pane move` is a no-op inside one tab, so re-nesting goes out to a scratch tab."""

    def test_dashboard_moves_out_and_back_under_the_captain(self):
        calls = []
        with patch.object(runtime, "herdr", lambda *call, **kw: calls.append(call) or {}):
            agents.renest_dashboard("w1:p2", PANE)
        self.assertEqual(calls[0], ("pane", "move", "w1:p2", "--new-tab", "--no-focus"))
        back = calls[1]
        self.assertEqual(back[:3], ("pane", "move", "w1:p2"))
        self.assertEqual(back[back.index("--tab") + 1], "w1:t1")
        self.assertEqual(back[back.index("--target-pane") + 1], "w1:p1")
        self.assertEqual(back[back.index("--split") + 1], "down")
        self.assertEqual(float(back[back.index("--ratio") + 1]), agents.DASHBOARD_RATIO)


class CrewRenestTests(LaunchHarness):
    """A crew pane in the captain's tab leaves the dashboard spanning it; re-nest it."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(Pane, "wait_for_crew", lambda *a: None))
        self.enterContext(patch.object(Pane, "submit_task", lambda *a: None))
        self.renests = []
        self.enterContext(
            patch.object(agents, "renest_dashboard", lambda *call: self.renests.append(call))
        )

    def recruit(self, *flags):
        _, _, env = self.execs[0]
        argv = ["--session", env["CAPTAIN_SESSION"], "crew", "jack", "--agent", "claude"]
        argv += ["--task", "build", *flags]
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            agents.create_crew(cli.parser().parse_args(argv), PANE, self.project)

    def test_crew_split_in_the_captain_tab_renests_the_dashboard(self):
        self.launch(["--agent", "claude"])
        self.recruit("--placement", "pane", "--direction", "vertical", "--split-pane", "w1:p1")
        self.assertEqual(self.renests, [("w1:p2", PANE)])

    def test_crew_in_another_tab_leaves_the_dashboard_alone(self):
        self.launch(["--agent", "claude"])
        self.recruit("--placement", "tab")
        self.assertEqual(self.renests, [])

    def test_no_dashboard_means_nothing_to_renest(self):
        self.launch(["--agent", "claude", "--no-dashboard"])
        self.recruit("--placement", "pane", "--direction", "vertical", "--split-pane", "w1:p1")
        self.assertEqual(self.renests, [])


class DashboardSplitTargetTests(unittest.TestCase):
    """The dashboard is a few rows of table; crew must never be split off it."""

    PANES = {"w1:p1": (0, 0, 480, 100), "w1:p2": (0, 100, 480, 20)}

    def setUp(self):
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def herdr(self, *call, **kwargs):
        if call[:2] == ("pane", "layout"):
            return {
                "layout": {
                    "panes": [
                        {"pane_id": p, "rect": dict(zip(("x", "y", "width", "height"), r))}
                        for p, r in self.PANES.items()
                    ]
                }
            }
        if call[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": "w1:t1", "label": "Captain Barbossa"}]}
        return {"panes": [{"pane_id": p, "tab_id": "w1:t1"} for p in self.PANES]}

    def placement(self):
        return Placement(PANE, Session(None, {"crew": {}}), "w1:p2")

    def test_auto_placement_ignores_the_dashboard_pane(self):
        with patch.object(runtime, "herdr", self.herdr):
            _, split_pane, _, _ = self.placement().auto_split()
        self.assertEqual(split_pane, "w1:p1")

    def test_the_pane_menu_omits_the_dashboard(self):
        with patch.object(runtime, "herdr", self.herdr):
            groups = self.placement().workspace_panes()
        self.assertEqual(set(groups["w1:t1"][1]), {"w1:p1"})

    def test_asking_for_the_dashboard_by_id_is_refused(self):
        args = SimpleNamespace(direction="vertical", split_pane="w1:p2")
        with patch.object(runtime, "herdr", self.herdr):
            with self.assertRaises(CaptainError):
                self.placement().choose_split(args, "pane")


if __name__ == "__main__":
    unittest.main()
