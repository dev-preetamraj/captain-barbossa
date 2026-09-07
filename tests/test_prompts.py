import unittest
from unittest.mock import patch

import questionary
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

    def test_only_the_focused_choice_is_highlighted(self):
        frames = []
        select = questionary.select
        with (
            create_pipe_input() as keyboard,
            create_app_session(input=keyboard, output=DummyOutput()),
            patch("sys.stdin.isatty", return_value=True),
        ):

            def capture(*args, **kwargs):
                prompt = select(*args, **kwargs)

                def rendered(app):
                    screen = app.renderer.last_rendered_screen
                    if app.is_done or screen is None:
                        return
                    cells = [cell for row in screen.data_buffer.values() for cell in row.values()]
                    frames.append(
                        (
                            "".join(c.char for c in cells if "class:highlighted" in c.style),
                            any("class:selected" in c.style for c in cells),
                            any(
                                app.renderer.style.get_attrs_for_style_str(c.style).reverse
                                for c in cells
                            ),
                        )
                    )
                    keyboard.send_text("j" if len(frames) == 1 else "\r")

                prompt.application.after_render += rendered
                return prompt

            with patch("captain_barbossa.prompts.questionary.select", side_effect=capture):
                self.assertEqual(
                    choose(None, ("claude", "codex"), "Choose your captain", "--agent"), "codex"
                )
        self.assertEqual(frames, [("Claude Code", False, False), ("Codex", False, False)])

    def test_explicit_choices_do_not_open_a_selector(self):
        with patch("captain_barbossa.prompts.questionary.select") as menu:
            self.assertEqual(
                choose("codex", ("claude", "codex"), "Choose your captain", "--agent"), "codex"
            )
            menu.assert_not_called()
