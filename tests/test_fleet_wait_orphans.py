"""An unackable orphaned background recruit never cuts `wait --all` short on its own."""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, sessions, store
from tests.home_isolation import SessionCase


class FleetWaitOrphanTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        # A background recruit whose recruiter died before begin reserved anything.
        self.current.meta["crew"]["jack"] = {
            "id": "jack",
            "name": "Jack",
            "agent": "jack",
            "provider": "codex",
            "incarnation_id": "first",
            "execution": "background",
            "status": "starting",
        }
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))

    def test_an_orphan_rides_along_the_full_wait(self):
        args = cli.parser().parse_args(
            ["--session", self.current.meta["id"], "wait", "--all", "--timeout", "30", "--json"]
        )
        empty = {"status": "timeout", "deliveries": []}
        with (
            patch.object(protocol, "wait_all", return_value=empty) as wait_all,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(args, self.pane, self.project)
        self.assertEqual(wait_all.call_args.args[1], 30)
        self.assertIn('"crew": "Jack"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
