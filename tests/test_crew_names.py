"""Crew names: automatic, unique, canonical, and released for reuse on dismiss."""

import contextlib
import io
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase

# create_crew's shell_ready_for_input polls a raw `pane read`, which needs text; a
# fixture built to return a dict for every herdr call would otherwise blow up on it.
SETTLED_SHELL_TEXT = "~/project $ "


def pane_stub(base):
    """Wrap a herdr stub so a `pane read` returns settled shell text instead of
    whatever `base` answers everything else with (a dict, `base` being callable or not)."""

    def api(*call, **kwargs):
        if call[:2] == ("pane", "read"):
            return SETTLED_SHELL_TEXT
        return base(*call, **kwargs) if callable(base) else base

    return api


class CrewNameTests(SessionCase):
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

    def test_automatic_names_are_unique_across_concurrent_recruits_and_session_scoped(self):
        self.meta["crew"] = {
            "jack": {"status": "needs_attention"},
            "scout": {"status": "started"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        args = self.args(
            "crew",
            "--agent",
            "codex",
            "--task",
            "standby",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
        ):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(
                    pool.map(
                        lambda _: agents.create_crew(args, self.pane, self.project),
                        range(len(agents.CREW_NAMES) + 1),
                    )
                )
            roster = store.read_json(self.directory / "session.json")["crew"]
            self.assertEqual(set(roster), set(agents.CREW_NAMES) | {"scout", "jack-1", "will-1"})
            for name, record in self.meta["crew"].items():
                self.assertEqual(roster[name], record)
            for name in roster.keys() - self.meta["crew"].keys():
                self.assertEqual(roster[name]["id"], name)
                self.assertEqual(len(roster[name]["name"].split()), 1)
                self.assertTrue((self.directory / f"crew-{name}.sh").is_file())
            self.assertEqual(roster["jack-1"]["name"], "Jack-1")
            self.assertEqual(roster["will-1"]["name"], "Will-1")
            other, meta = sessions.session(self.project, self.pane, create=True)
            args.session = meta["id"]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                agents.create_crew(args, self.pane, self.project)
            result = json.loads(output.getvalue())
            self.assertEqual(result["id"], "jack")
            self.assertEqual(result["name"], "Jack")
            self.assertEqual(set(store.read_json(other / "session.json")["crew"]), {"jack"})

    def test_one_canonical_name_is_used_by_recruit_dismiss_and_memory(self):
        for name in agents.CREW_NAMES:
            with self.subTest(name=name):
                self.assertEqual(name, name.capitalize().casefold())
                self.assertNotIn("-", name)
        args = self.args(
            "crew",
            "--agent",
            "codex",
            "--task",
            "standby",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)) as api,
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.create_crew(args, self.pane, self.project)
        record = json.loads(output.getvalue())
        self.assertEqual(record["id"], "jack")
        self.assertEqual(record["name"], "Jack")
        self.assertEqual(record["agent"], f"c-{self.meta['id'][:8]}-jack")
        self.assertIn(("pane", "rename", "w1:p2", "Jack"), [c.args for c in api.call_args_list])
        crew = Crew("jack", record, sessions.Session(self.directory, self.meta))
        unread_messages = protocol.unread(crew)
        protocol.receipt(
            crew.session.directory, crew.crew_id, [msg["id"] for msg in unread_messages]
        )
        protocol.change(
            crew,
            self.args(
                "done",
                "Jack",
                "--assignment",
                record["assignment_id"],
                "--report",
                "No files changed; checks passed; finished",
            ),
            self.project,
        )
        delivery = protocol.poll(crew)
        protocol.poll(crew, delivery["delivery_id"])
        with (
            patch.object(runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as dismissed,
        ):
            agents.dismiss_crew(self.args("dismiss", "Jack"), self.pane, self.project)
        self.assertEqual(dismissed.getvalue(), "Dismissed Jack.\n")
        labels = [node["label"] for node in store.read_json(self.directory / "graph.json")["nodes"]]
        self.assertIn("Jack", labels)
        self.assertIn(record["agent"], labels)
        self.assertNotIn("Barbossa", labels)

    def test_dismissed_crew_release_their_names_for_reuse(self):
        self.meta["crew"] = {
            "jack": {"status": "dismissed", "name": "Jack", "agent": "c-session-jack"},
            "will": {"status": "started"},
            "elizabeth": {"status": "needs_attention"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                agents.create_crew(
                    self.args(
                        "crew",
                        "--agent",
                        "codex",
                        "--task",
                        "standby",
                        "--placement",
                        "pane",
                        "--direction",
                        "vertical",
                        "--split-pane",
                        "w1:p1",
                    ),
                    self.pane,
                    self.project,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(
                (result["id"], result["name"], result["status"]), ("jack", "Jack", "started")
            )
            roster = store.read_json(self.directory / "session.json")["crew"]
            self.assertEqual(set(roster), {"jack", "will", "elizabeth"})
            self.assertEqual(roster["jack"]["agent"], result["agent"])
            self.assertEqual(
                (roster["jack"]["task"], roster["jack"]["status"]), ("standby", "started")
            )
            with self.assertRaisesRegex(runtime.CaptainError, "already exists"):
                agents.create_crew(
                    self.args(
                        "crew",
                        "jack",
                        "--agent",
                        "codex",
                        "--task",
                        "x",
                        "--placement",
                        "pane",
                        "--direction",
                        "vertical",
                        "--split-pane",
                        "w1:p1",
                    ),
                    self.pane,
                    self.project,
                )
            roster["jack"]["status"] = "dismissed"
            store.write_json(self.directory / "session.json", {**self.meta, "crew": roster})
            crew = Crew("jack", roster["jack"], sessions.Session(self.directory, self.meta))
            unread_messages = protocol.unread(crew)
            protocol.receipt(
                crew.session.directory, crew.crew_id, [msg["id"] for msg in unread_messages]
            )
            protocol.change(
                crew,
                self.args(
                    "done",
                    "Jack",
                    "--assignment",
                    result["assignment_id"],
                    "--report",
                    "No files changed; checks passed; handoff",
                ),
                self.project,
            )
            delivery = protocol.poll(crew)
            protocol.poll(crew, delivery["delivery_id"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                agents.create_crew(
                    self.args(
                        "crew",
                        "jack",
                        "--agent",
                        "codex",
                        "--task",
                        "again",
                        "--handoff",
                        result["assignment_id"],
                        "--placement",
                        "pane",
                        "--direction",
                        "vertical",
                        "--split-pane",
                        "w1:p1",
                    ),
                    self.pane,
                    self.project,
                )
            self.assertEqual(json.loads(output.getvalue())["name"], "Jack")

    def test_dismissing_a_crew_frees_its_name_for_the_next_auto_recruit(self):
        args = self.args(
            "crew",
            "--agent",
            "codex",
            "--task",
            "standby",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
        ):
            for _ in range(3):
                agents.create_crew(args, self.pane, self.project)
        roster = store.read_json(self.directory / "session.json")["crew"]
        self.assertEqual(set(roster), {"jack", "will", "elizabeth"})
        crew = Crew("jack", roster["jack"], sessions.Session(self.directory, self.meta))
        unread_messages = protocol.unread(crew)
        protocol.receipt(
            crew.session.directory, crew.crew_id, [msg["id"] for msg in unread_messages]
        )
        protocol.change(
            crew,
            self.args(
                "done",
                "Jack",
                "--assignment",
                roster["jack"]["assignment_id"],
                "--report",
                "No files changed; checks passed; finished",
            ),
            self.project,
        )
        delivery = protocol.poll(crew)
        protocol.poll(crew, delivery["delivery_id"])
        with patch.object(runtime, "herdr", return_value={}):
            agents.dismiss_crew(self.args("dismiss", "Jack"), self.pane, self.project)
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.create_crew(args, self.pane, self.project)
        result = json.loads(output.getvalue())
        self.assertEqual((result["id"], result["name"]), ("jack", "Jack"))

    def test_named_handoff_still_reuses_a_dismissed_name(self):
        args = self.args(
            "crew",
            "--agent",
            "codex",
            "--task",
            "standby",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
        ):
            agents.create_crew(args, self.pane, self.project)
        roster = store.read_json(self.directory / "session.json")["crew"]
        old_assignment_id = roster["jack"]["assignment_id"]
        crew = Crew("jack", roster["jack"], sessions.Session(self.directory, self.meta))
        unread_messages = protocol.unread(crew)
        protocol.receipt(
            crew.session.directory, crew.crew_id, [msg["id"] for msg in unread_messages]
        )
        protocol.change(
            crew,
            self.args(
                "done",
                "Jack",
                "--assignment",
                old_assignment_id,
                "--report",
                "No files changed; checks passed; finished",
            ),
            self.project,
        )
        delivery = protocol.poll(crew)
        protocol.poll(crew, delivery["delivery_id"])
        with patch.object(runtime, "herdr", return_value={}):
            agents.dismiss_crew(self.args("dismiss", "Jack"), self.pane, self.project)
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.create_crew(
                self.args(
                    "crew",
                    "jack",
                    "--agent",
                    "codex",
                    "--task",
                    "again",
                    "--handoff",
                    old_assignment_id,
                    "--placement",
                    "pane",
                    "--direction",
                    "vertical",
                    "--split-pane",
                    "w1:p1",
                ),
                self.pane,
                self.project,
            )
        result = json.loads(output.getvalue())
        self.assertEqual((result["id"], result["name"]), ("jack", "Jack"))

    def test_non_character_names_are_rejected_before_launch(self):
        for name in ("scout", "barbossa", "../../jack", "", "jack;ls", "jack-123456789012"):
            with self.subTest(name=name), patch.object(runtime, "herdr") as api:
                args = self.args("crew", name, "--task", "standby")
                with self.assertRaisesRegex(runtime.CaptainError, "omit NAME"):
                    agents.create_crew(args, self.pane, self.project)
                api.assert_not_called()
        self.assertEqual(store.read_json(self.directory / "session.json")["crew"], {})


if __name__ == "__main__":
    unittest.main()
