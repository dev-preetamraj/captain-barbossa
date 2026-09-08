"""Regression tests: a launched captain/crew child must resolve the same state and
temp roots as its parent, even though the child sees a fresh process environment."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, memory


class ChildMemoryRootTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.temp_root = self.root / "temp"
        self.temp_root.mkdir()
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                    "CAPTAIN_STATE_ROOT": str(self.state_root),
                    "CAPTAIN_TEMP_ROOT": str(self.temp_root),
                },
                clear=True,
            )
        )
        self.enterContext(patch.object(agents, "READY_POLLS", 1))
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *extra):
        return cli.parser().parse_args(["--session", self.meta["id"], *extra])

    @staticmethod
    def env_flag(flags, key):
        prefix = f"{key}="
        return next(flag[len(prefix) :] for flag in flags if flag.startswith(prefix))

    def test_launch_forwards_the_parents_distinct_state_and_temp_roots(self):
        with (
            patch.object(agents, "herdr", return_value={}),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(agents.sys.stdin, "isatty", return_value=True),
            patch.object(os, "execvpe") as execute,
        ):
            agents.launch(self.args("--agent", "claude"), self.pane, self.project)
        _, _, env = execute.call_args.args
        self.assertEqual(env["CAPTAIN_STATE_ROOT"], str(memory.state_root()))
        self.assertEqual(env["CAPTAIN_TEMP_ROOT"], str(memory.temp_root()))
        self.assertNotEqual(env["CAPTAIN_STATE_ROOT"], env["CAPTAIN_TEMP_ROOT"])

    def test_create_crew_forwards_the_parents_distinct_state_and_temp_roots(self):
        created = {
            "pane": {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
            "root_pane": {"pane_id": "w1:p3"},
            "tab_id": "w1:t9",
            "agent": {"name": f"c-{self.meta['id'][:8]}-jack", "agent_status": "working"},
        }
        args = self.args("crew", "--agent", "claude", "--task", "build", "--placement", "tab")
        with (
            patch.object(agents, "herdr", return_value=created) as api,
            patch.object(agents, "executable", return_value="/bin/claude"),
        ):
            agents.create_crew(args, self.pane, self.project)
        tab_create = next(call for call in api.call_args_list if call.args[:2] == ("tab", "create"))
        state_flag = self.env_flag(tab_create.args, "CAPTAIN_STATE_ROOT")
        temp_flag = self.env_flag(tab_create.args, "CAPTAIN_TEMP_ROOT")
        self.assertEqual(state_flag, str(memory.state_root()))
        self.assertEqual(temp_flag, str(memory.temp_root()))
        self.assertNotEqual(state_flag, temp_flag)

    def test_child_env_resolves_project_graph_under_the_parents_state_root_not_temp(self):
        with (
            patch.object(agents, "herdr", return_value={}),
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(agents.sys.stdin, "isatty", return_value=True),
            patch.object(os, "execvpe") as execute,
        ):
            agents.launch(self.args("--agent", "claude"), self.pane, self.project)
        _, _, parent_env = execute.call_args.args
        parent_state_storage = memory.state_storage(self.project)
        memory.add_memory(parent_state_storage / "graph.json", "project", "uses", "Python")

        # Simulate the child's own process env: only what launch() actually forwards,
        # plus a different HOME, to prove state resolution doesn't fall back to XDG
        # defaults (which would silently land the child on a different directory).
        child_env = {
            "CAPTAIN_SESSION": parent_env["CAPTAIN_SESSION"],
            "CAPTAIN_PROJECT": parent_env["CAPTAIN_PROJECT"],
            "CAPTAIN_STATE_ROOT": parent_env["CAPTAIN_STATE_ROOT"],
            "CAPTAIN_TEMP_ROOT": parent_env["CAPTAIN_TEMP_ROOT"],
            "HOME": str(self.root / "different-home"),
        }
        with patch.dict(os.environ, child_env, clear=True):
            child_state_storage = memory.state_storage(self.project)
            self.assertEqual(child_state_storage, parent_state_storage)
            self.assertNotEqual(child_state_storage, memory.storage(self.project))
            graph = memory.read_json(child_state_storage / "graph.json")
        self.assertEqual(len(graph["links"]), 1)


if __name__ == "__main__":
    unittest.main()
