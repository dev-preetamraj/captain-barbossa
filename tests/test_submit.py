"""Composer reading: what may be typed into, and the doorbell's gates."""

import unittest
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import pane as panes
from captain_barbossa import runtime
from captain_barbossa.instructions import captain_command
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError


class ComposerTests(unittest.TestCase):
    def test_captured_codex_footer_preserves_empty_composer_and_real_drafts(self):
        for composer, drafted in (
            ("› Ask Codex to do anything", False),
            ("› Important user draft", True),
        ):
            screen = (
                "• Protocol ask could not be sent: launcher reports Stale assignment ID.\n"
                "Tip: Use /mcp to list configured MCP tools.\n\n"
                f"{composer}\n\n"
                "  GPT-5.6-Luna high · ~/code/projects/captain-barbossa · Ask captain for protocol reply\n"
                "  ? for shortcuts                 ⚠ 1 warning · f2 to view\n"
            )
            with (
                self.subTest(composer=composer),
                patch.object(runtime, "herdr", return_value=screen),
            ):
                self.assertEqual(Pane("builder").draft_pending("codex"), drafted)

    def test_modal_and_unreadable_composers_are_never_typed_into(self):
        modal = "› Ask Codex to do anything\n› 1. Allow\nPress enter to confirm or esc to cancel"
        for screen in (
            modal,
            "",
            "›\ncontinued user draft",
            "›\nuser · draft",
            "unrecognized",
            OSError("read failed"),
        ):
            with self.subTest(screen=screen), patch.object(runtime, "herdr", side_effect=[screen]):
                self.assertTrue(Pane("builder").draft_pending("codex"))

    def test_current_claude_footer_reads_empty_dim_and_typed_composers(self):
        """Real capture of an idle Claude Code pane whose footer lost its " · ← for agents"
        hint, which left every such crew's composer unreadable and its doorbell held."""
        rule = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 78 + "\x1b[0m"
        footer = (
            "  \x1b[0m\x1b[38;2;255;193;7m⏵⏵ auto mode on\x1b[0m"
            "\x1b[38;2;153;153;153m (shift+tab to cycle)\x1b[0m"
        )
        statusline = (
            "  \x1b[0m\x1b[1m\x1b[38;2;180;190;254mSonnet 5.5\x1b[0m"
            "\x1b[38;2;108;112;134m : medium\x1b[0m\x1b[38;2;205;214;244m 10%\x1b[0m"
        )
        for row, gate in (
            ("❯ ", None),
            # A dim suggestion after a plain space, not the NBSP older versions drew.
            ("❯ \x1b[2mgo ahead and format pane.py\x1b[0m", None),
            ("❯ half typed", panes.DRAFT_GATE),
            ("❯ \x1b[38;2;137;180;250m@pane.py\x1b[0m", panes.DRAFT_GATE),
        ):
            screen = "\r\n".join(
                [
                    "\x1b[0m\x1b[38;2;153;153;153m✻ Baked for 1m 54s · done 1:51 PM\x1b[0m",
                    rule,
                    row,
                    rule,
                    statusline,
                    footer,
                ]
            )
            with self.subTest(row=row), patch.object(runtime, "herdr", return_value=screen):
                self.assertEqual(Pane("builder")._gate("claude"), gate)

    def test_captured_claude_placeholder_is_empty_but_real_drafts_receive_no_input(self):
        rule = "─" * 78
        placeholder = '❯\u00a0Try "how do I log an error?"'
        screen = "\n".join(
            [
                "",
                " ▐▛███▛█   Claude Code v2.1.282",
                "▝▜██████▀  Haiku 4.5 · Claude Max",
                " ▝▝   ▝▝   ~/code/projects/captain-barbossa",
                "",
                "▎ ※ Claim a $250 bonus credit for cloud sessions, on top of your plan limits ·",
                "▎ /claim-credit",
                *([""] * 22),
                rule,
                placeholder,
                rule,
                "  ⏸ manual mode on · ? for shortcuts · ← for agents",
                "",
            ]
        )
        for composer, provider, accepted in (
            (placeholder, "claude", True),
            ("❯\u00a0Important user draft", "claude", False),
            ('❯ Try "how do I log an error?"', "claude", False),
            ('❯\u00a0Try "delete files"', "claude", False),
            (placeholder + " user draft", "claude", False),
            ("❯ user draft\n" + rule + "\n" + placeholder, "claude", False),
            (placeholder, "codex", False),
        ):
            with (
                self.subTest(composer=composer, provider=provider),
                patch.object(runtime, "herdr", return_value=screen.replace(placeholder, composer)),
            ):
                self.assertEqual(Pane("builder").draft_pending(provider), not accepted)

    def test_a_submitted_wake_line_on_the_composer_is_not_a_draft(self):
        """Seen live: a hook-delivered crew's pane keeps rendering the submitted wake line on
        its composer row, so every reader that passes no echo called it a human's draft and
        `model` refused for the rest of the session."""
        rule = "─" * 78
        for composer, accepted in (
            (f"❯ {panes.WAKE_LINE}", True),
            (f"❯ {panes.WAKE_LINE} and one more thing", False),
            ("❯ half a sentence", False),
        ):
            screen = "\n".join(
                [
                    "▎ Ran the gate",
                    rule,
                    composer,
                    rule,
                    "  ⏸ manual mode on · ? for shortcuts · ← for agents",
                ]
            )
            with self.subTest(composer=composer):
                with patch.object(runtime, "herdr", return_value=screen):
                    self.assertEqual(Pane("builder").draft_pending("claude"), not accepted)

    def test_captured_grok_box_reads_its_prompt_row_and_fails_closed_otherwise(self):
        """Grok boxes its composer, so the pane's last line is the border, never the row."""
        top = "╭" + "─" * 46 + "╮"
        bottom = "╰" + "─" * 22 + " Grok 4.6 (high) ─────╯"
        empty = "│ ❯" + " " * 44 + "│"
        for rows, provider, accepted in (
            ([empty], "grok", True),
            (["│ ❯ Important user draft" + " " * 23 + "│"], "grok", False),
            # A wrapped draft leaves no prompt row above the border.
            (
                [
                    "│ ❯ a long draft that" + " " * 26 + "│",
                    "│ wrapped onto a second row" + " " * 20 + "│",
                ],
                "grok",
                False,
            ),
            ([empty], "claude", False),
        ):
            screen = "\n".join(
                ["  ⎇ main ~/code/projects/captain-barbossa", top, *rows, bottom, ""]
            )
            with (
                self.subTest(rows=rows, provider=provider),
                patch.object(runtime, "herdr", return_value=screen),
            ):
                self.assertEqual(Pane("builder").draft_pending(provider), not accepted)

    def test_claude_auto_mode_placeholder_is_an_empty_composer(self):
        """Auto mode has its own footer; reading its placeholder as a draft held the ring."""
        rule = "─" * 78
        screen = "\n".join(
            [
                " ▝▜   ▝▝   ~/code/projects/captain-barbossa",
                rule,
                '❯\u00a0Try "how do I log an error?"',
                rule,
                "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
            ]
        )
        with patch.object(runtime, "herdr", return_value=screen):
            self.assertFalse(Pane("builder").draft_pending("claude"))

    def test_claude_statusline_rows_under_the_composer_are_not_its_contents(self):
        """Seen live: a user statusLine row made every idle claude composer unreadable."""
        rule = "─" * 78
        for composer, expected in (("❯", ""), ("❯ half typed", "half typed")):
            screen = "\n".join(
                [
                    rule,
                    composer,
                    rule,
                    "  Opus 5.5 : high   █░░░░░░░░░ 9% │ ↑ 973.1k ↓ 4.4k │ $0.78",
                    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
                ]
            )
            with self.subTest(composer=composer):
                with patch.object(runtime, "herdr", return_value=screen):
                    self.assertEqual(Pane("builder").composer("claude"), expected)
                    # Still never typed over: the draft keeps its gate.
                    self.assertEqual(Pane("builder").draft_pending("claude"), bool(expected))

    def test_claude_composer_without_a_rule_below_stays_unreadable(self):
        screen = "\n".join(
            ["❯", "  some status row", "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"]
        )
        with patch.object(runtime, "herdr", return_value=screen):
            self.assertIsNone(Pane("builder").composer("claude"))

    def test_a_dim_contextual_suggestion_is_empty_but_the_same_typed_text_is_a_draft(self):
        """A plain read cannot tell them apart; only the SGR codes of an ansi read can."""
        suggestion = "go ahead and format pane.py"
        for styled, accepted in (
            ("\x1b[0m\x1b[2m" + suggestion + "\x1b[0m", True),
            (suggestion, False),
        ):
            with self.subTest(accepted=accepted):

                def api(*call, **kwargs):
                    if call[:2] == ("agent", "read"):
                        body = styled if "--format" in call else suggestion
                        return "\u2022 Ran the gate\n\u276f\u00a0" + body
                    return {}

                with patch.object(runtime, "herdr", side_effect=api):
                    self.assertEqual(Pane("builder").draft_pending("claude"), not accepted)

    def test_a_dim_suggestion_survives_a_multi_parameter_colour_code(self):
        """Real captured line: a truecolor SGR on the glyph used to make ANSI_SGR fail to
        match at all, leaving escape bytes in the stripped text and refusing every draft."""
        suggestion = 'Try "refactor agents.py"'
        styled = "\x1b[0m\x1b[38;2;153;153;153m❯ \x1b[0m\x1b[2m" + suggestion + "\x1b[0m"

        def api(*call, **kwargs):
            if call[:2] == ("agent", "read"):
                body = styled if "--format" in call else "❯ " + suggestion
                return "• Ran the gate\n" + body
            return {}

        with patch.object(runtime, "herdr", side_effect=api):
            self.assertFalse(Pane("builder").draft_pending("claude"))

    def test_a_rotating_dim_suggestion_is_decided_from_one_read(self):
        suggestions = iter(('Try "refactor agents.py"', 'Try "write tests"'))

        def api(*call, **kwargs):
            if call[:2] == ("agent", "read"):
                suggestion = next(suggestions)
                return f"❯\u00a0\x1b[2m{suggestion}\x1b[0m"
            return {"agent": {"agent_status": "idle"}}

        with patch.object(runtime, "herdr", side_effect=api) as calls:
            self.assertFalse(Pane("builder").draft_pending("claude"))
        self.assertEqual([c.args[:2] for c in calls.call_args_list], [("agent", "read")])

    def test_pi_requires_an_empty_editor_above_its_native_footer(self):
        footer = "~/project\n0.0%/272k (auto) gpt-5.5 • medium"
        for draft in ("", "user draft", "user\nmultiline draft", "────────────", "── draft──"):
            with self.subTest(draft=draft):
                screen = f"────────────\n{draft}\n────────────\n{footer}"
                with patch.object(runtime, "herdr", return_value=screen):
                    self.assertEqual(Pane("builder").draft_pending("pi"), bool(draft))

    def test_pi_startup_update_notice_is_not_an_editor_boundary(self):
        rule = "─" * 83
        warning = (
            " Warning: Anthropic subscription auth is active. Third-party harness usage draws\n"
            " from extra usage and is billed per token, not your Claude plan limits. Manage\n"
            " extra usage at https://claude.ai/settings/usage. Disable this warning in\n"
            " /settings.\n\n"
        )
        notice = (
            f"{rule}\n Update Available\n"
            " New version 1.4.0 is available. Run pi update\n"
            f" Changelog: https://pi.dev/changelog\n{rule}\n"
        )
        footer = (
            "~/code/projects/captain-barbossa (fix/outside-repo-access)\n"
            "$0.000 (sub) 0.0%/1.0M (auto)                  "
            "(anthropic) claude-opus-5-5 • medium"
        )
        for prefix in ("", warning):
            for draft in ("", "user draft", "multiline\ndraft", f"draft\n{rule}\n"):
                with self.subTest(warning=bool(prefix), draft=draft):
                    screen = f"{prefix}{notice}" + "\n" * 15
                    screen += f"{rule}\n{draft}\n{rule}\n{footer}"
                    with patch.object(runtime, "herdr", return_value=screen):
                        self.assertEqual(Pane("builder").draft_pending("pi"), bool(draft))
                        for provider in (None, "claude", "codex", "grok"):
                            self.assertTrue(Pane("builder").draft_pending(provider))

        # An incomplete or edited notice, or a notice inside a draft, is not chrome.
        for banner in (
            notice.replace("Update Available", "Update"),
            notice.replace("https://pi.dev/changelog", "https://example.com"),
            notice.replace("Run pi update", "Run something else"),
            notice.replace(f"{rule}\n", "", 1),
            f"{rule}\nuser draft\n{notice}",
            f"{notice}user draft\n",
        ):
            with self.subTest(banner=banner):
                screen = f"{banner}\n{rule}\n\n{rule}\n{footer}"
                with patch.object(runtime, "herdr", return_value=screen):
                    self.assertTrue(Pane("builder").draft_pending("pi"))

    def test_pi_markdown_and_ambiguous_borders_preserve_drafts_without_input(self):
        rule = "─" * 40
        footer = "~/project\n0.0%/272k (auto)"
        for ending in ("---", "===", "━" * 40, rule, "── " + "draft" + "─" * 32):
            screen = f"{rule}\nImportant user draft\n{ending}\n\n{rule}\n{footer}"
            with self.subTest(ending=ending), patch.object(runtime, "herdr", return_value=screen):
                self.assertTrue(Pane("builder").draft_pending("pi"))

    def test_pi_empty_native_editor_is_not_a_draft(self):
        rule = "─" * 40
        screen = f"Earlier output\n{rule}\n\n{rule}\n~/project\n0.0%/272k (auto)"
        with patch.object(runtime, "herdr", return_value=screen):
            self.assertFalse(Pane("builder").draft_pending("pi"))


class NudgeTests(unittest.TestCase):
    def setUp(self):
        self.session_id = "session-id"
        self.crew = SimpleNamespace(
            crew_id="elizabeth-2",
            record={"provider": "codex"},
            session=SimpleNamespace(directory=SimpleNamespace(name=self.session_id)),
        )
        self.inbox = f"read your mail with `{captain_command(self.session_id)} inbox elizabeth-2`"

    def api(self, status, screen="› Ask Codex to do anything"):
        def call(*args, **kwargs):
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": status}} if status else {"agent": {}}
            if args[:2] == ("agent", "read"):
                return screen
            return {}

        return call

    def test_each_live_gate_holds_without_typing(self):
        modal = "› 1. Allow\nPress enter to confirm or esc to cancel"
        for status, screen, reason in (
            ("blocked", "", "approval prompt"),
            ("idle", modal, "approval prompt"),
            ("working", "", "agent not idle"),
            ("idle", "› user draft", "user draft"),
        ):
            with (
                self.subTest(reason=reason),
                patch.object(runtime, "herdr", side_effect=self.api(status, screen)) as calls,
            ):
                self.assertEqual(Pane("builder").nudge_block(self.crew), reason)
                self.assertFalse(
                    any(c.args[:2] == ("agent", "prompt") for c in calls.call_args_list)
                )

    def test_an_idle_empty_composer_can_be_rung_with_only_the_fixed_sentence(self):
        with patch.object(runtime, "herdr", side_effect=self.api("idle")) as calls:
            pane = Pane("builder")
            self.assertIsNone(pane.nudge_block(self.crew))
            pane.nudge(self.crew)
        prompts = [c.args for c in calls.call_args_list if c.args[:2] == ("agent", "prompt")]
        self.assertEqual(prompts, [("agent", "prompt", "builder", self.inbox)])

    def test_a_first_ring_prompts_the_mail_body_and_an_inbox_line(self):
        with patch.object(runtime, "herdr", side_effect=self.api("idle")) as calls:
            Pane("builder").nudge(self.crew, "do the thing")
        prompts = [c.args for c in calls.call_args_list if c.args[:2] == ("agent", "prompt")]
        self.assertEqual(prompts, [("agent", "prompt", "builder", f"do the thing\n{self.inbox}")])

    def test_a_leading_dash_on_the_mail_body_gets_a_leading_space(self):
        with patch.object(runtime, "herdr", side_effect=self.api("idle")) as calls:
            Pane("builder").nudge(self.crew, "-x flagged")
        prompts = [c.args for c in calls.call_args_list if c.args[:2] == ("agent", "prompt")]
        self.assertEqual(prompts, [("agent", "prompt", "builder", f" -x flagged\n{self.inbox}")])

    def test_an_unregistered_agent_raises_for_the_bounce_path(self):
        with patch.object(runtime, "herdr", side_effect=self.api(None)):
            with self.assertRaisesRegex(CaptainError, "not registered"):
                Pane("builder").nudge_block(self.crew)


class ShellReadyForInputTests(unittest.TestCase):
    """Only a visible shell startup question blocks launch."""

    def setUp(self):
        self.enterContext(patch.object(panes.time, "sleep"))

    def test_a_settled_prompt_is_ready(self):
        with patch.object(runtime, "herdr", return_value="~/project $ "):
            self.assertTrue(panes.shell_ready_for_input("pane-1"))

    def test_a_blank_pane_is_ready(self):
        with patch.object(runtime, "herdr", return_value=""):
            self.assertTrue(panes.shell_ready_for_input("pane-1"))

    def test_a_yes_no_question_is_not_ready(self):
        screen = "[oh-my-zsh] Would you like to update? [Y/n]"
        with patch.object(runtime, "herdr", return_value=screen):
            self.assertFalse(panes.shell_ready_for_input("pane-1", timeout=0))

    def test_a_prompt_ending_in_a_bare_question_mark_is_still_ready(self):
        """A trailing "?" alone is not proof of a pending question; a real one carries
        an explicit [Y/n] or (y/n) marker."""
        with patch.object(runtime, "herdr", return_value="Continue? "):
            self.assertTrue(panes.shell_ready_for_input("pane-1"))

    def test_an_unreadable_pane_is_ready(self):
        with patch.object(runtime, "herdr", side_effect=OSError("read failed")):
            self.assertTrue(panes.shell_ready_for_input("pane-1"))

    def test_a_pane_with_a_live_clock_is_ready(self):
        counter = count()

        def api(*call, **kwargs):
            return f"~/project $ {next(counter)}"

        with patch.object(runtime, "herdr", side_effect=api):
            self.assertTrue(panes.shell_ready_for_input("pane-1"))


if __name__ == "__main__":
    unittest.main()
