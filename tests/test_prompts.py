import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from captain_barbossa.prompts import choose
from captain_barbossa.runtime import CaptainError


class SelectorTests(unittest.TestCase):
    def select(self, keys, options=("claude", "codex")):
        with (
            create_pipe_input() as keyboard,
            create_app_session(input=keyboard, output=DummyOutput()),
            patch("sys.stdin.isatty", return_value=True),
        ):
            keyboard.send_text(keys)
            return choose(None, options, "Choose your captain", "--agent")

    def test_navigation_and_enter_return_the_underlying_choice(self):
        for keys, expected in (
            ("\r", "claude"),
            ("j\r", "codex"),
            ("jk\r", "claude"),
            ("\x1b[B\r", "codex"),
            ("\x1b[B\x1b[A\r", "claude"),
            ("k\r", "codex"),
            ("xj\r", "codex"),
        ):
            with self.subTest(keys=repr(keys)):
                self.assertEqual(self.select(keys), expected)
        self.assertEqual(self.select("j\r", ("pane", "tab")), "tab")

    def test_escape_eof_and_interrupt_cancel(self):
        for key in ("\x1b", "\x04"):
            with self.subTest(key=repr(key)), self.assertRaisesRegex(CaptainError, "cancelled"):
                self.select(key)
        with self.assertRaises(KeyboardInterrupt):
            self.select("\x03")

    def test_explicit_choices_do_not_open_a_selector(self):
        with patch("captain_barbossa.prompts.questionary.select") as menu:
            self.assertEqual(
                choose("codex", ("claude", "codex"), "Choose your captain", "--agent"), "codex"
            )
            menu.assert_not_called()
