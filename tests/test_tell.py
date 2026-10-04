import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from tests.home_isolation import HERDR, SessionCase


class TellCrewTests(SessionCase):
    """Regression tests for follow-up prompts (agents.tell_crew)."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": self.agent_name,
                "provider": "claude",
                "task": "build",
                "pane": "w1:p2",
                "tab": "w1:t1",
                "status": "started",
            }
        }
        store.write_json(self.directory / "session.json", self.meta)
        self.events = store.private_dir(self.directory / "events") / "jack.jsonl"
        self.events.write_text("", encoding="utf-8")

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def tell(self, name, message):
        sent = []

        def api(*args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                sent.append(args[2:])
                return {}
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "working"}}
            if args[:2] == ("agent", "read"):
                return "❯"
            raise AssertionError(f"unexpected herdr call: {args}")

        with (
            patch.object(runtime, "herdr", side_effect=api),
            patch.object(panes.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.tell_crew(self.args("tell", name, message), self.pane, self.project)
        return sent, output.getvalue()

    def append_event(self, event):
        with self.events.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")

    def test_follow_up_is_submitted_and_recorded(self):
        sent, output = self.tell("jack", "now do the docs")
        self.assertEqual(sent, [(self.agent_name, "now do the docs")])
        self.assertIn("Sent to Jack.", output)
        meta = store.read_json(self.directory / "session.json")
        self.assertEqual(meta["crew"]["jack"]["task"], "now do the docs")
        graph = store.read_json(self.directory / "graph.json")
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        assigned = [
            (labels[link["source"]], labels[link["target"]])
            for link in graph["links"]
            if link["relation"] == "assigned"
        ]
        self.assertIn(("Jack", "now do the docs"), assigned)

    def test_stale_idle_event_is_consumed_before_the_prompt_lands(self):
        self.append_event({"hook_event_name": "Notification", "notification_type": "idle_prompt"})
        self.tell("Jack", "keep going")
        cursor = self.events.with_suffix(".cursor")
        self.assertEqual(store.read_json(cursor), self.events.stat().st_size)
        with patch.object(runtime, "herdr", side_effect=AssertionError):
            status, _ = Crew(
                "jack",
                {"agent": self.agent_name, "task": "keep going"},
                sessions.Session(self.directory, self.meta),
            ).status(0)
        self.assertIsNone(status)

    def test_dismissed_crew_is_refused(self):
        meta = store.read_json(self.directory / "session.json")
        meta["crew"]["jack"]["status"] = "dismissed"
        store.write_json(self.directory / "session.json", meta)
        with self.assertRaisesRegex(agents.CaptainError, "dismissed"):
            self.tell("Jack", "keep going")

    def test_blank_message_is_refused(self):
        with self.assertRaisesRegex(agents.CaptainError, "1–8000 characters"):
            self.tell("Jack", "   ")


if __name__ == "__main__":
    unittest.main()
