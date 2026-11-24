import contextlib
import io
import json
import os
from unittest.mock import patch

from captain_barbossa import cli, events, protocol, sessions, store
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class CrewGrantTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {"name": "Jack", "agent": "jack", "incarnation_id": "first", "provider": "claude"},
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        self.crew.events.parent.mkdir(parents=True, exist_ok=True)

    def send(self, hook, command, **extra):
        event = {
            "hook_event_name": hook,
            "session_id": "native",
            "tool_use_id": "toolu_1",
            "cwd": str(self.project),
            "tool_name": "Bash",
            "tool_input": {"command": command},
            **extra,
        }
        with contextlib.redirect_stdout(io.StringIO()) as output:
            events.append_event([str(self.crew.events), json.dumps(event)])
        logged = json.loads(self.crew.events.read_text().splitlines()[-1])
        return json.loads(output.getvalue() or "{}"), logged

    def grants(self):
        return protocol.active_assignment(self.crew).get("grants", [])

    def test_a_grant_allows_its_exact_command_once(self):
        protocol.grant(self.crew, self.assignment["id"], "git push origin feature")
        output, event = self.send("PreToolUse", "git push origin main")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        output, event = self.send("PreToolUse", "git push origin feature")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(event["approval"]["code"], "captain-grant")
        self.assertEqual(self.grants(), [])
        output, event = self.send("PreToolUse", "git push origin feature", tool_use_id="toolu_2")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(event["approval"]["code"], "flagged-outward")

    def test_codex_spends_a_grant_at_pretooluse_and_honours_it_once_at_permission_request(self):
        self.crew.record["provider"] = "codex"
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.grant(self.crew, self.assignment["id"], "git push")
        output, event = self.send("PreToolUse", "git push", turn_id="turn-1")
        self.assertEqual(output, {})  # Codex treats a PreToolUse allow as unsupported.
        self.assertEqual(event["approval"]["code"], "captain-grant")
        request = {"tool_use_id": None, "turn_id": "turn-1"}
        output, event = self.send("PermissionRequest", "git push", **request)
        self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        self.assertEqual(event["approval"]["pretool"], "toolu_1")
        output, _ = self.send("PermissionRequest", "git push", **request)
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_a_grant_is_bound_to_the_live_assignment_and_never_inherited(self):
        with self.assertRaisesRegex(CaptainError, "Stale assignment"):
            protocol.grant(self.crew, "other", "git push")
        protocol.grant(self.crew, self.assignment["id"], "git push")
        protocol.grant(self.crew, self.assignment["id"], "git push")
        self.assertEqual(len(self.grants()), 1)
        with protocol.checkpoint(self.current.directory) as state:
            state["assignments"][self.assignment["id"]].update(state="done", report="r")
        with self.assertRaisesRegex(CaptainError, "done"):
            protocol.grant(self.crew, self.assignment["id"], "git status")
        successor = protocol.begin(self.crew, self.project, "next", handoff=self.assignment["id"])
        self.assertNotIn("grants", successor)

    def test_a_grant_spent_at_pretooluse_is_not_honoured_after_a_handoff(self):
        self.crew.record["provider"] = "codex"
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.grant(self.crew, self.assignment["id"], "git push")
        _, event = self.send("PreToolUse", "git push", turn_id="turn-1")
        self.assertEqual(event["approval"]["assignment"], self.assignment["id"])
        with protocol.checkpoint(self.current.directory) as state:
            state["assignments"][self.assignment["id"]].update(state="done", report="r")
        protocol.begin(self.crew, self.project, "next", handoff=self.assignment["id"])
        request = {"tool_use_id": None, "turn_id": "turn-1"}
        output, event = self.send("PermissionRequest", "git push", **request)
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(event["approval"]["code"], "flagged-outward")

    def test_a_grant_is_spent_on_its_first_match_even_when_not_needed(self):
        protocol.grant(self.crew, self.assignment["id"], "git status")
        _, event = self.send("PreToolUse", "git status")
        self.assertEqual(event["approval"]["code"], "granted")
        self.assertEqual(self.grants(), [])

    def test_crew_cannot_grant(self):
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew", "CAPTAIN_CREW": "jack"}):
            with self.assertRaisesRegex(CaptainError, "captain-only"):
                protocol.grant(self.crew, self.assignment["id"], "git push")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.main(["grant", "Jack", "--command", "x", "--assignment", "a"])
        self.assertEqual(code, 1)
        self.assertIn("captain-only", err.getvalue())

    def test_the_grant_verb_records_the_command(self):
        with (
            contextlib.chdir(self.project),
            patch.object(cli, "current_pane", return_value=self.pane),
            patch.object(cli, "pump_mail"),
            patch.dict(os.environ, {"CAPTAIN_SESSION": self.current.meta["id"]}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            argv = ["--session", self.current.meta["id"], "grant", "Jack"]
            code = cli.main(
                [*argv, "--command", "npm publish", "--assignment", self.assignment["id"]]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["grants"], 1)
        self.assertEqual(self.grants()[0]["command"], "npm publish")
