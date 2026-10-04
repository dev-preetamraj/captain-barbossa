"""Task submission: a crew counts as started only once its composer is empty."""

import subprocess
import unittest
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import pane as panes
from captain_barbossa import runtime
from captain_barbossa.instructions import captain_command
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError

TASK = "build the thing"


class CodexPane:
    """A Codex pane that reports working while it renames its own thread.

    Herdr sees the renaming turn as activity even though the task is still an unsent
    draft in the composer. `readable` off hides the composer after sending.
    """

    def __init__(self, status="working", swallows_enter=False, readable=True):
        self.status = status
        self.draft = False
        self.sent = False
        self.swallows_enter = swallows_enter
        self.readable = readable
        self.calls = []

    def __call__(self, *call, **kwargs):
        self.calls.append(call)
        if call[:2] == ("agent", "read"):
            return self.text()
        if call[:2] == ("agent", "prompt"):
            self.sent = True
            self.draft = self.swallows_enter
        if call[:2] == ("agent", "send-keys") and not self.swallows_enter:
            self.draft = False
            self.status = "working"
        if call[:2] == ("agent", "get"):
            return {"agent": {"name": "builder", "agent_status": self.status}}
        return {}

    def text(self):
        if self.sent and not self.readable:
            return "unrecognized composer"
        if self.draft:
            composer = f"› {TASK}" if self.readable else f"  {TASK}"
        else:
            composer = "› Ask Codex to do anything"
        return "\n".join(["• Ran uv run --locked python -m unittest -q", composer])

    def verbs(self):
        return [call[:2] for call in self.calls]


class SubmitTaskTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(panes.time, "sleep"))
        self.enterContext(patch.object(panes.time, "monotonic", side_effect=count(0, 2)))

    def submit(self, pane, provider="codex"):
        with patch.object(runtime, "herdr", side_effect=pane):
            Pane("builder").submit_task(TASK, provider)

    def test_a_renaming_codex_with_the_task_still_drafted_is_not_reported_started(self):
        pane = CodexPane(swallows_enter=True)
        with self.assertRaisesRegex(CaptainError, "outcome is unknown"):
            self.submit(pane)
        self.assertNotIn(("agent", "send-keys"), pane.verbs())
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)

    def test_delayed_native_state_uses_one_send(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                elapsed = 0
                pane = CodexPane(status="idle")

                def advance(seconds):
                    nonlocal elapsed
                    elapsed += seconds
                    if elapsed >= 8:
                        pane.status = "working"

                with (
                    patch.object(runtime, "herdr", side_effect=pane),
                    patch.object(panes.time, "monotonic", side_effect=lambda: elapsed),
                    patch.object(panes.time, "sleep", side_effect=advance),
                ):
                    Pane("builder").submit_task(TASK, provider)
                self.assertGreaterEqual(elapsed, 8)
                self.assertLess(elapsed, 10)
                self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
                self.assertNotIn(("agent", "send-keys"), pane.verbs())

    def test_observation_expires_without_another_send(self):
        pane = CodexPane(status="idle")
        ticks = count(0, 0.2)
        with (
            patch.object(runtime, "herdr", side_effect=pane),
            patch.object(panes.time, "monotonic", side_effect=ticks),
        ):
            with self.assertRaisesRegex(CaptainError, "outcome is unknown"):
                Pane("builder").submit_task(TASK, "codex")
        self.assertLess(next(ticks), 25)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertNotIn(("agent", "send-keys"), pane.verbs())

    def test_captured_codex_footer_preserves_empty_composer_and_real_drafts(self):
        for composer in ("› Ask Codex to do anything", "› Important user draft"):
            with self.subTest(composer=composer):
                pane = CodexPane()
                screen = (
                    "• Protocol ask could not be sent: launcher reports Stale assignment ID.\n"
                    "Tip: Use /mcp to list configured MCP tools.\n\n"
                    f"{composer}\n\n"
                    "  GPT-5.6-Luna high · ~/code/projects/captain-barbossa · Ask captain for protocol reply\n"
                    "  ? for shortcuts                 ⚠ 1 warning · f2 to view\n"
                )

                def api(*call, **kwargs):
                    if call[:2] == ("agent", "read"):
                        return screen
                    return pane(*call, **kwargs)

                with patch.object(runtime, "herdr", side_effect=api):
                    if composer == "› Ask Codex to do anything":
                        Pane("builder").submit_task(TASK, "codex")
                        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
                    else:
                        with self.assertRaisesRegex(CaptainError, "no task was sent"):
                            Pane("builder").submit_task(TASK, "codex")
                        self.assertNotIn(("agent", "prompt"), pane.verbs())
                    self.assertNotIn(("agent", "send-keys"), pane.verbs())

    def test_idle_after_send_is_uncertain_and_never_retried(self):
        pane = CodexPane(status="idle")
        with self.assertRaisesRegex(CaptainError, "outcome is unknown"):
            self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 0)

    def test_existing_user_draft_receives_neither_text_nor_enter(self):
        pane = CodexPane()
        pane.draft = True
        with self.assertRaisesRegex(CaptainError, "no task was sent"):
            self.submit(pane)
        self.assertNotIn(("agent", "prompt"), pane.verbs())
        self.assertNotIn(("agent", "send-keys"), pane.verbs())

    def test_unreadable_composer_after_send_is_not_treated_as_empty(self):
        pane = CodexPane(readable=False)
        with self.assertRaisesRegex(CaptainError, "outcome is unknown"):
            self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 0)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)

    def test_an_already_submitted_codex_task_receives_no_extra_enter(self):
        pane = CodexPane()
        pane.draft = False
        self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 0)

    def test_prompt_starting_with_a_dash_gets_a_leading_space(self):
        """A task like "-x" must reach the agent as text, not get parsed as a flag; herdr
        has no "--" terminator (verified against the real binary), so a leading space guards it."""
        pane = CodexPane()
        pane.draft = False
        with patch.object(runtime, "herdr", side_effect=pane):
            Pane("builder").submit_task("-x does a thing", "codex")
        prompt_call = next(call for call in pane.calls if call[:2] == ("agent", "prompt"))
        self.assertEqual(prompt_call[2:], ("builder", " -x does a thing"))

    def test_prompt_without_a_leading_dash_is_passed_through_unchanged(self):
        pane = CodexPane()
        pane.draft = False
        with patch.object(runtime, "herdr", side_effect=pane):
            Pane("builder").submit_task(TASK, "codex")
        prompt_call = next(call for call in pane.calls if call[:2] == ("agent", "prompt"))
        self.assertEqual(prompt_call[2:], ("builder", TASK))

    def test_blocked_modal_and_unreadable_panes_receive_no_input(self):
        modal = "› Ask Codex to do anything\n› 1. Allow\nPress enter to confirm or esc to cancel"
        for screen in (
            modal,
            "",
            "›\ncontinued user draft",
            "›\nuser · draft",
            "unrecognized",
            OSError("read failed"),
        ):
            with self.subTest(screen=screen):
                pane = CodexPane()

                def api(*call, **kwargs):
                    if call[:2] == ("agent", "read"):
                        if isinstance(screen, Exception):
                            raise screen
                        return screen
                    return pane(*call, **kwargs)

                with patch.object(runtime, "herdr", side_effect=api):
                    with self.assertRaises(CaptainError):
                        Pane("builder").submit_task(TASK, "codex")
                self.assertNotIn(("agent", "prompt"), pane.verbs())
                self.assertNotIn(("agent", "send-keys"), pane.verbs())
        pane = CodexPane(status="blocked")
        with self.assertRaises(CaptainError):
            self.submit(pane)
        self.assertNotIn(("agent", "prompt"), pane.verbs())

    def test_send_timeout_is_unknown_and_is_never_retried(self):
        pane = CodexPane()

        def api(*call, **kwargs):
            result = pane(*call, **kwargs)
            if call[:2] == ("agent", "prompt"):
                raise subprocess.TimeoutExpired("herdr", 15)
            return result

        with patch.object(runtime, "herdr", side_effect=api):
            with self.assertRaisesRegex(CaptainError, "outcome is unknown"):
                Pane("builder").submit_task(TASK, "claude")
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertNotIn(("agent", "send-keys"), pane.verbs())

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
            with self.subTest(composer=composer, provider=provider):
                sent = False

                def api(*call, **kwargs):
                    nonlocal sent
                    if call[:2] == ("agent", "read"):
                        return screen.replace(placeholder, composer)
                    if call[:2] == ("agent", "prompt"):
                        sent = True
                    return {"agent": {"agent_status": "working" if sent else "idle"}}

                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    self.assertEqual(Pane("builder").draft_pending(provider), not accepted)
                    if accepted:
                        Pane("builder").submit_task(TASK, provider)
                    else:
                        with self.assertRaisesRegex(CaptainError, "no task was sent"):
                            Pane("builder").submit_task(TASK, provider)
                self.assertEqual(
                    [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
                    [("agent", "prompt", "builder", TASK)] if accepted else [],
                )

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
            with self.subTest(rows=rows, provider=provider):
                screen = "\n".join(
                    ["  ⎇ main ~/code/projects/captain-barbossa", top, *rows, bottom, ""]
                )
                sent = False

                def api(*call, **kwargs):
                    nonlocal sent
                    if call[:2] == ("agent", "read"):
                        return screen
                    if call[:2] == ("agent", "prompt"):
                        sent = True
                    return {"agent": {"agent_status": "working" if sent else "idle"}}

                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    self.assertEqual(Pane("builder").draft_pending(provider), not accepted)
                    if accepted:
                        Pane("builder").submit_task(TASK, provider)
                    else:
                        with self.assertRaisesRegex(CaptainError, "no task was sent"):
                            Pane("builder").submit_task(TASK, provider)
                self.assertEqual(
                    [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
                    [("agent", "prompt", "builder", TASK)] if accepted else [],
                )

    def test_claude_auto_mode_placeholder_is_an_empty_composer(self):
        """Auto mode has its own footer; reading its placeholder as a draft sent no task."""
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
        sent = False

        def api(*call, **kwargs):
            nonlocal sent
            if call[:2] == ("agent", "read"):
                return screen
            if call[:2] == ("agent", "prompt"):
                sent = True
            return {"agent": {"agent_status": "working" if sent else "idle"}}

        with patch.object(runtime, "herdr", side_effect=api) as calls:
            Pane("builder").submit_task(TASK, "claude")
        self.assertEqual(
            [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
            [("agent", "prompt", "builder", TASK)],
        )

    def test_a_dim_contextual_suggestion_is_empty_but_the_same_typed_text_is_a_draft(self):
        """A plain read cannot tell them apart; only the SGR codes of an ansi read can."""
        suggestion = "go ahead and format pane.py"
        for styled, accepted in (
            ("\x1b[0m\x1b[2m" + suggestion + "\x1b[0m", True),
            (suggestion, False),
        ):
            with self.subTest(accepted=accepted):
                sent = False

                def api(*call, **kwargs):
                    nonlocal sent
                    if call[:2] == ("agent", "read"):
                        body = styled if "--format" in call else suggestion
                        return "\u2022 Ran the gate\n\u276f\u00a0" + body
                    if call[:2] == ("agent", "prompt"):
                        sent = True
                    return {"agent": {"agent_status": "working" if sent else "idle"}}

                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    self.assertEqual(Pane("builder").draft_pending("claude"), not accepted)
                    if accepted:
                        Pane("builder").submit_task(TASK, "claude")
                    else:
                        with self.assertRaisesRegex(CaptainError, "no task was sent"):
                            Pane("builder").submit_task(TASK, "claude")
                self.assertEqual(
                    [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
                    [("agent", "prompt", "builder", TASK)] if accepted else [],
                )

    def test_a_dim_suggestion_survives_a_multi_parameter_colour_code(self):
        """Real captured line: a truecolor SGR on the glyph used to make ANSI_SGR fail to
        match at all, leaving escape bytes in the stripped text and refusing every draft."""
        suggestion = 'Try "refactor agents.py"'
        styled = "\x1b[0m\x1b[38;2;153;153;153m❯ \x1b[0m\x1b[2m" + suggestion + "\x1b[0m"
        sent = False

        def api(*call, **kwargs):
            nonlocal sent
            if call[:2] == ("agent", "read"):
                body = styled if "--format" in call else "❯ " + suggestion
                return "• Ran the gate\n" + body
            if call[:2] == ("agent", "prompt"):
                sent = True
            return {"agent": {"agent_status": "working" if sent else "idle"}}

        with patch.object(runtime, "herdr", side_effect=api) as calls:
            self.assertFalse(Pane("builder").draft_pending("claude"))
            Pane("builder").submit_task(TASK, "claude")
        self.assertEqual(
            [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
            [("agent", "prompt", "builder", TASK)],
        )

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

    def test_a_queued_message_mid_turn_lands_without_a_resend(self):
        """Claude Code queues a prompt that arrives mid-turn and flushes it when the turn
        ends; agent_status reads idle the whole time, so this trio is the only landing proof."""
        empty = "\n".join(
            [
                "─" * 78,
                '❯ Try "how do I log an error?"',
                "─" * 78,
                "  ⏸ manual mode on · ? for shortcuts · ← for agents",
            ]
        )
        queued = "\n".join(
            [
                TASK,
                "ctrl+enter to send now",
                "Press up to edit queued messages",
                "esc to interrupt",
            ]
        )
        sent = False

        def api(*call, **kwargs):
            nonlocal sent
            if call[:2] == ("agent", "read"):
                return queued if sent else empty
            if call[:2] == ("agent", "prompt"):
                sent = True
            return {"agent": {"agent_status": "idle"}}

        with patch.object(runtime, "herdr", side_effect=api) as calls:
            Pane("builder").submit_task(TASK, "claude")
        self.assertEqual(
            [c.args for c in calls.call_args_list if c.args[1] in ("prompt", "send-keys")],
            [("agent", "prompt", "builder", TASK)],
        )

    def test_pi_requires_an_empty_editor_above_its_native_footer(self):
        footer = "~/project\n0.0%/272k (auto) gpt-5.5 • medium"
        for draft in ("", "user draft", "user\nmultiline draft", "────────────", "── draft──"):
            with self.subTest(draft=draft):
                screen = f"────────────\n{draft}\n────────────\n{footer}"
                with patch.object(runtime, "herdr", return_value=screen):
                    self.assertEqual(Pane("builder").draft_pending("pi"), bool(draft))

    def test_pi_markdown_and_ambiguous_borders_preserve_drafts_without_input(self):
        rule = "─" * 40
        footer = "~/project\n0.0%/272k (auto)"
        for ending in ("---", "===", "━" * 40, rule, "── " + "draft" + "─" * 32):
            with self.subTest(ending=ending):
                screen = f"{rule}\nImportant user draft\n{ending}\n\n{rule}\n{footer}"

                def api(*call, **kwargs):
                    if call[:2] == ("agent", "read"):
                        return screen
                    return {"agent": {"agent_status": "idle"}}

                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    self.assertTrue(Pane("builder").draft_pending("pi"))
                    with self.assertRaisesRegex(CaptainError, "no task was sent"):
                        Pane("builder").submit_task(TASK, "pi")
                self.assertFalse(
                    any(call.args[1] in ("prompt", "send-keys") for call in calls.call_args_list)
                )

    def test_pi_empty_native_editor_still_accepts_one_prompt(self):
        rule = "─" * 40
        screen = f"Earlier output\n{rule}\n\n{rule}\n~/project\n0.0%/272k (auto)"

        def api(*call, **kwargs):
            if call[:2] == ("agent", "read"):
                return screen
            return {"agent": {"agent_status": "working"}}

        with patch.object(runtime, "herdr", side_effect=api) as calls:
            Pane("builder").submit_task(TASK, "pi")
        self.assertEqual(
            [call.args for call in calls.call_args_list if call.args[1] in ("prompt", "send-keys")],
            [("agent", "prompt", "builder", TASK)],
        )


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
