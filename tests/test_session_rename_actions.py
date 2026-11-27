"""A rename in a supervised session is logged as an action; a Herdr session logs nothing."""

import contextlib
import io
import json
import unittest

from home_isolation import SessionCase

from captain_barbossa import cli, eventlog, instructions, sessions, supervisor
from captain_barbossa.runtime import CaptainError


class SessionRenameActionTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)
        self.directory = self.current.directory

    def actions(self, directory=None):
        path = (directory or self.directory) / eventlog.ACTIONS
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_first_rename_logs_the_action_with_no_previous_name(self):
        sessions.rename_session(self.directory, " Fix login ")
        (action,) = self.actions()
        self.assertEqual(
            {key: action[key] for key in ("kind", "session_id", "name", "previous")},
            {
                "kind": "session-renamed",
                "session_id": self.directory.name,
                "name": "Fix login",
                "previous": None,
            },
        )

    def test_rename_logs_the_previous_name(self):
        sessions.rename_session(self.directory, "First")
        sessions.rename_session(self.directory, "Second")
        self.assertEqual(
            [(row["name"], row["previous"]) for row in self.actions()],
            [("First", None), ("Second", "First")],
        )

    def test_if_unnamed_on_a_named_session_logs_nothing_and_keeps_the_name(self):
        sessions.rename_session(self.directory, "Mine")
        sessions.rename_session(self.directory, "Captain's idea", if_unnamed=True)
        self.assertEqual(sessions.session_name(self.directory), "Mine")
        self.assertEqual(len(self.actions()), 1)

    def test_if_unnamed_on_an_unnamed_session_logs_the_rename(self):
        sessions.rename_session(self.directory, "Titled", if_unnamed=True)
        (action,) = self.actions()
        self.assertEqual((action["name"], action["previous"]), ("Titled", None))

    def test_renaming_to_the_same_name_logs_nothing(self):
        sessions.rename_session(self.directory, "Same")
        sessions.rename_session(self.directory, "Same")
        self.assertEqual(len(self.actions()), 1)

    def test_an_invalid_name_logs_nothing(self):
        with self.assertRaises(CaptainError):
            sessions.rename_session(self.directory, "x" * 61)
        self.assertEqual(self.actions(), [])

    def test_cli_rename_logs_the_action(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--session", self.directory.name, "--rename", "From CLI"])
        self.assertEqual(code, 0)
        (action,) = self.actions()
        self.assertEqual((action["name"], action["previous"]), ("From CLI", None))

    def test_herdr_session_rename_writes_no_action(self):
        herdr = sessions.session(self.project, self.pane, create=True)
        sessions.rename_session(herdr.directory, "Herdr title")
        self.assertEqual(sessions.session_name(herdr.directory), "Herdr title")
        self.assertEqual(self.actions(herdr.directory), [])
        self.assertFalse((herdr.directory / eventlog.ACTION_SPOOL).exists())

    def test_unnamed_supervised_captain_gets_the_same_naming_instruction_as_herdr(self):
        for provider in ("codex", "claude", "pi"):
            with self.subTest(provider=provider):
                herdr = instructions.agent_instructions(
                    self.directory, "Captain Barbossa", provider
                )
                app = instructions.agent_instructions(
                    self.directory, "Captain Barbossa", provider, supervised=True
                )
                self.assertIn("Unnamed session", app)
                self.assertIn("--rename 'TITLE' --if-unnamed", app)
                self.assertEqual(self.naming(app), self.naming(herdr))

    def test_named_supervised_captain_gets_no_naming_instruction(self):
        sessions.rename_session(self.directory, "Named")
        app = instructions.agent_instructions(
            self.directory, "Captain Barbossa", "codex", supervised=True
        )
        self.assertNotIn("Unnamed session", app)

    @staticmethod
    def naming(text):
        start = text.index("Unnamed session")
        return text[start : text.index("\n", text.index("TITLE is", start))]


if __name__ == "__main__":
    unittest.main()
