"""`captain status` lists a foreground crew whose recruiter died before reserving it."""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase


class StatusUnreservedCrewTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        # A worktree recruit publishes its roster entry before protocol.begin.
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": "c-abc-jack",
                "provider": "claude",
                "incarnation_id": "i1",
                "worktree_id": "wt1",
                "checkout": "worktree",
                "task": "build the thing",
            },
        }
        store.write_json(self.directory / "session.json", self.meta)
        store.write_json(self.directory / "protocol.json", {"active": {}, "assignments": {}})

    def test_status_lists_a_crew_with_no_reserved_assignment(self):
        args = cli.parser().parse_args(["--session", self.meta["id"], "status"])
        with (
            patch.object(runtime, "herdr", return_value={"agent": {"agent_status": "idle"}}),
            patch.object(Pane, "lines", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.status_crew(args, self.pane, self.project)
        line = next(line for line in output.getvalue().splitlines() if line.startswith("Jack"))
        self.assertIn("build the thing", line)


if __name__ == "__main__":
    unittest.main()
