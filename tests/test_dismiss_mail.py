import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, protocol, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase


class DismissMailTests(SessionCase):
    """Regression: dismiss_crew() must bounce mail a crew never got to read, not drop it."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.directory = self.current.directory
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "incarnation_id": "first",
                "pane": "w1:p2",
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None, create=True))
        self.enterContext(patch.object(Pane, "nudge", create=True))
        self.assignment = protocol.begin(self.crew, self.project, "original", ["src"], ["edit"])

    def mail(self, message_id):
        path = protocol.mail_dir(self.directory, self.crew.crew_id) / f"{message_id}.json"
        return store.read_json(path)

    def dismiss(self):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        return output.getvalue()

    def test_dismiss_bounces_mail_the_crew_never_read(self):
        """A sent message the crew never read leaves its mail file queued; dismiss must bounce it."""
        message_id = protocol.deliver(self.crew, "original", initial=True)
        with protocol.checkpoint(self.directory) as state:
            assignment = protocol.active(state, self.crew, self.assignment["id"])
            assignment["state"] = "done"
        self.assertEqual(self.mail(message_id)["state"], "queued")
        output = self.dismiss()
        self.assertIn(message_id, output)
        self.assertIn("Dismissed Jack.", output)
        mail = self.mail(message_id)
        self.assertEqual(mail["state"], "bounced")
        self.assertIn("dismiss", mail["reason"].lower())

    def test_dismiss_with_no_mail_prints_no_bounce_line(self):
        with protocol.checkpoint(self.directory) as state:
            assignment = protocol.active(state, self.crew, self.assignment["id"])
            assignment["state"] = "done"
        output = self.dismiss()
        self.assertNotIn("Bounced", output)
        self.assertIn("Dismissed Jack.", output)


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


class DismissCrewTests(SessionCase):
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

    def test_dismiss_closes_pane_marks_record_and_records_memory(self):
        self.meta["crew"] = {
            "jack": {
                "name": "Jack",
                "agent": "c-session-jack",
                "pane": "w1:p2",
                "placement": "pane",
                "status": "started",
            },
            "will": {
                "name": "Will",
                "agent": "c-session-will",
                "pane": "w1:p3",
                "placement": "tab",
                "status": "started",
            },
        }
        store.write_json(self.directory / "session.json", self.meta)
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(runtime, "herdr", return_value={}) as api,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["--session", self.meta["id"], "dismiss", " jAcK "]), 0)
        api.assert_called_once_with("pane", "close", "w1:p2")
        self.assertEqual(output.getvalue(), "Dismissed Jack.\n")
        saved = store.read_json(self.directory / "session.json")
        self.assertEqual(saved["crew"]["jack"]["status"], "dismissed")
        self.assertEqual(saved["crew"]["jack"]["pane"], "w1:p2")
        self.assertEqual(saved["crew"]["will"], self.meta["crew"]["will"])
        graph = store.read_json(self.directory / "graph.json")
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        self.assertEqual(
            [
                (labels[link["source"]], link["relation"], labels[link["target"]])
                for link in graph["links"]
            ],
            [(f"session:{self.meta['id']}", "dismissed", "c-session-jack")],
        )
        with patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(runtime.CaptainError, "already dismissed"):
                agents.dismiss_crew(self.args("dismiss", "Jack"), self.pane, self.project)
            api.assert_not_called()

    def test_a_crew_holding_only_unread_mail_dismisses_cleanly(self):
        """Mail that never reached the crew cannot demand a done report from it."""
        created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle"}}
        with (
            patch.object(runtime, "herdr", side_effect=pane_stub(created)),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(Pane, "wait_for_crew"),
            patch.object(Pane, "submit_task"),
        ):
            agents.create_crew(
                self.args(
                    "crew",
                    "jack",
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
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        crew = Crew("jack", record, sessions.Session(self.directory, self.meta))
        self.assertTrue(protocol.unread(crew))
        with (
            patch.object(runtime, "herdr", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(self.args("dismiss", "Jack"), self.pane, self.project)
        self.assertIn("Bounced unread mail to Jack", output.getvalue())
        self.assertIn("Dismissed Jack.", output.getvalue())
        self.assertEqual(
            store.read_json(self.directory / "session.json")["crew"]["jack"]["status"],
            "dismissed",
        )
        # The reservation closes too, or the dismissed crew's owned paths stay locked.
        assignment = store.read_json(self.directory / "protocol.json")["assignments"][
            record["assignment_id"]
        ]
        self.assertEqual(assignment["state"], "done")
        self.assertEqual(assignment["report"], "Dismissed before any message was read.")

    def test_dismiss_failures_leave_the_record_and_memory_untouched(self):
        self.meta["crew"] = {
            "jack": {"name": "Jack", "agent": "c-session-jack", "pane": "w1:p2"},
            "legacy-jack": {
                "name": "Jack",
                "agent": "c-session-legacy-jack",
                "pane": "w1:p3",
            },
            "cotton": {"name": "Cotton", "agent": "c-session-cotton"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        with patch.object(runtime, "herdr") as api:
            for name, message in (
                ("Elizabeth", "Available crew"),
                ("Jack", "ambiguous"),
                ("Cotton", "no recorded pane"),
            ):
                with self.subTest(name=name), self.assertRaisesRegex(runtime.CaptainError, message):
                    agents.dismiss_crew(self.args("dismiss", name), self.pane, self.project)
                api.assert_not_called()
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(runtime, "herdr", side_effect=runtime.CaptainError("boom")) as api,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(
                cli.main(["--session", self.meta["id"], "dismiss", "c-session-jack"]), 1
            )
            self.assertIn("Could not dismiss Jack: boom", error.getvalue())
            api.assert_called_once_with("pane", "close", "w1:p2")
        self.assertEqual(store.read_json(self.directory / "session.json"), self.meta)
        self.assertFalse((self.directory / "graph.json").exists())

    def test_dismiss_treats_a_pane_already_gone_as_already_closed(self):
        """pane_not_found means someone else already closed it; the record still retires."""
        self.meta["crew"] = {
            "jack": {"name": "Jack", "agent": "c-session-jack", "pane": "w1:p2"},
        }
        store.write_json(self.directory / "session.json", self.meta)
        with (
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "project_root", return_value=self.project),
            patch.object(
                runtime, "herdr", side_effect=runtime.CaptainError("pane_not_found")
            ) as api,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                cli.main(["--session", self.meta["id"], "dismiss", "c-session-jack"]), 0
            )
            self.assertEqual(output.getvalue(), "Dismissed Jack.\n")
            api.assert_called_once_with("pane", "close", "w1:p2")
        roster = store.read_json(self.directory / "session.json")["crew"]
        self.assertEqual(roster["jack"]["status"], "dismissed")
        graph = store.read_json(self.directory / "graph.json")
        self.assertIn("c-session-jack", [node["label"] for node in graph["nodes"]])


if __name__ == "__main__":
    unittest.main()
