import contextlib
import io
import json
import os
import shlex
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, config, models, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import HERDR, SessionCase

CODEX_PICKER = """
Select Model and Effort

› 1. gpt-6-astra (current)  Our most capable model for complex, demanding work.
  2. gpt-5.6-sol            Reliable agentic workhorse for everyday tasks.
  3. gpt-5.6-terra          Balanced agentic coding model for everyday work.
  4. gpt-5.6-luna           Fast and affordable agentic coding.
  5. gpt-5.5                Proven previous generation.

Press enter to confirm or esc to go back
"""
CODEX_EFFORT = """
Select Reasoning Level for gpt-5.6-sol

  1. Low               Fast responses with lighter reasoning
› 2. Medium (default)  Balances speed and reasoning depth for everyday tasks

Press enter to confirm or esc to go back
"""
# Claude Code's own /model picker, verbatim: the model in use is marked and focused, the
# scroll arrow of the truncated list sits in the focus column, the recommended row
# describes itself with another model's name, and two versions of a family are listed.
CLAUDE_FOOTER = "Enter to set as default · s to use this session only · Esc to cancel"
CLAUDE_IN_USE = 7
CLAUDE_MODELS = (
    ("Default (recommended)", "Opus 5 · Best for everyday, complex tasks"),
    ("Opus 5.5", "For complex work and everyday tasks"),
    ("Fable 5.1", "For your toughest challenges"),
    ("Sonnet 5.5", "Most efficient for simpler tasks"),
    ("Haiku 4.5", "Fastest for quick answers"),
    ("Sonnet 5", "Efficient for routine tasks"),
    ("Opus 5 ✔", "Best for everyday, complex tasks"),
    ("Fable 5", "Most capable for your hardest and longest-running tasks"),
    ("Opus 4.8", "Best for everyday, complex tasks"),
    ("Opus 4.7", "Best for everyday, complex tasks"),
)


def claude_picker(focused, footer=CLAUDE_FOOTER, models=CLAUDE_MODELS, hidden=2):
    """The picker with "❯" on row number `focused`, and nothing else telling rows apart."""
    rows = []
    for number, (name, description) in enumerate(models, start=1):
        mark = "❯" if number == focused else "↓" if number == len(models) and hidden else " "
        rows.append(f"{mark} {str(number) + '.':<4}{name:<22}  {description}")
    return "\n".join(
        [
            "Select model",
            "Switch between Claude models. Your pick becomes the default for new sessions. "
            "For other/previous model names, specify with --model.",
            "",
            *rows,
            *([f"   … +{hidden} models"] if hidden else []),
            "",
            "● High effort (default) ←/→ to adjust",
            "",
            "Use /fast to turn on Fast mode (Opus 5.5).",
            "",
            footer,
        ]
    )


def claude_modal(name, yes_focused=True):
    """Claude Code's confirm modal for a switch that re-reads the conversation, verbatim."""
    mark = "❯ " if yes_focused else "  "
    return "\n".join(
        [
            "Switch model?",
            "Your next response will be slower and use more tokens",
            "",
            "This conversation is cached for the current model. Switching to "
            f"{name} means the full history gets re-read on your next message.",
            "",
            f"{mark}1. Yes, switch to {name}",
            "  2. No, go back",
        ]
    )


class SwitchModelTests(SessionCase):
    """Regression tests for mid-session model switching (agents.switch_model)."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes.time, "sleep"))
        # Real deadlines: keep the polling loops short instead of waiting them out.
        self.enterContext(patch.object(agents, "MODEL_TIMEOUT", 0.05))
        self.enterContext(patch.object(agents, "PROMPT_TIMEOUT", 0.05))
        self.enterContext(patch.object(panes, "CLAUDE_PICKER_STEP", 0.01))
        self.enterContext(patch.object(panes, "CLAUDE_PICKER_PAINT", 0.01))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def crew(self, provider, model="claude-haiku-4-5"):
        agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": agent_name,
                "provider": provider,
                "pane": "w1:p2",
                "tab": "w1:t1",
                "model": model,
                "status": "started",
            }
        }
        store.write_json(self.directory / "session.json", self.meta)
        return agent_name

    def switch(self, api, target, refuses=None):
        """Switch Jack to target, returning the herdr mock. `refuses` expects that message."""
        args = cli.parser().parse_args(["--session", self.meta["id"], "model", "Jack", target])
        with patch.object(runtime, "herdr", side_effect=api) as herdr:
            if refuses:
                with self.assertRaisesRegex(CaptainError, refuses):
                    agents.switch_model(args, self.pane, self.project)
            else:
                agents.switch_model(args, self.pane, self.project)
        return herdr

    def reader(self, screens, status="idle", composer=None):
        """Answer pane reads from screens, advancing when the driven keys are sent."""
        state = {"screen": 0}

        def api(*args, **kwargs):
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": status}}
            if args[:2] == ("agent", "read"):
                if state["screen"] == 0:
                    return (
                        composer
                        if composer is not None
                        else (
                            "────────\n\n────────\n~/project\n0.0%/272k (auto)"
                            if self.meta["crew"]["jack"]["provider"] == "pi"
                            else "❯"
                        )
                    )
                return screens[min(state["screen"], len(screens) - 1)]
            if args[:2] in (("agent", "send-keys"), ("agent", "prompt")):
                state["screen"] += 1
            return {}

        return api

    def claude_screens(self, focused, confirmation="", modal=None):
        """One picker screen per keypress the drive sends, the fake advancing on each.

        `focused` is the row number focus lands on after each move, starting from the one
        the picker opens on. The screen after `s` is the confirm modal when there is one,
        and the last screen is what the confirmation reads.
        """
        moves = [claude_picker(row) for row in (CLAUDE_IN_USE, *focused)]
        return ["", *moves, *([modal] if modal else []), confirmation]

    def test_claude_switch_moves_focus_onto_the_row_it_was_asked_for(self):
        """Inline `/model <id>` retiers the crew but also saves the user's global default."""
        agent_name = self.crew("claude")
        # Sonnet 5 is row 6 and the picker opens on row 7, so the one move is upwards.
        herdr = self.switch(
            self.reader(self.claude_screens([6], "⎿  Set model to Sonnet 5 for this session only")),
            "mid",
        )
        calls = [call.args for call in herdr.call_args_list]
        self.assertIn(("agent", "prompt", agent_name, "/model"), calls)
        self.assertEqual([call[3:] for call in calls if call[1] == "send-keys"], [("up",), ("s",)])
        self.assertFalse([c for c in calls if any("claude-sonnet-5" in str(arg) for arg in c)])
        self.assertIn("Jack switched to claude-sonnet-5.", self.output.getvalue())
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-sonnet-5")

    def test_focus_is_moved_one_key_at_a_time_and_only_towards_the_row(self):
        self.crew("claude")
        # Haiku 4.5 is row 5: two rows up, read back one keypress at a time.
        herdr = self.switch(
            self.reader(
                self.claude_screens([6, 5], "Set model to Haiku 4.5 for this session only")
            ),
            "cheap",
        )
        keys = [call.args[3:] for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(keys, [("up",), ("up",), ("s",)])
        self.assertIn("Jack switched to claude-haiku-4-5.", self.output.getvalue())

    def test_the_row_in_use_needs_no_move_and_its_own_mark_is_not_its_label(self):
        """Opus 5 is row 7, marked in use and focused; Opus 5.5 on row 2 is another model."""
        self.crew("claude")
        herdr = self.switch(
            self.reader(self.claude_screens([], "Set model to Opus 5 for this session only")),
            "strong",
        )
        keys = [call.args[3:] for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(keys, [("s",)])
        self.assertIn("Jack switched to claude-opus-5.", self.output.getvalue())

    def picker_api(self, before, after, unfocused_reads=0):
        """A herdr stand-in for one /model pick: `before` is the picker's pane until `s`,
        and `after` the pane once it is pressed. `before` takes whether focus is drawn, and
        the first `unfocused_reads` picker reads show none, as a pane still painting would."""
        state = {"opened": False, "reads": 0, "pressed": []}

        def api(*args, **kwargs):
            if args[:2] == ("agent", "get"):
                return {"agent": {"agent_status": "idle"}}
            if args[:2] == ("agent", "prompt"):
                state["opened"] = True
            if args[:2] == ("agent", "send-keys"):
                state["pressed"].append(args[3:])
            if args[:2] == ("agent", "read"):
                if not state["opened"]:
                    return "❯"
                if state["pressed"][-1:] == [("s",)]:
                    return after
                state["reads"] += 1
                return before(state["reads"] > unfocused_reads)
            return {}

        return api, state

    def refuses(self, screens, target, message):
        """A switch that must refuse: the keys it sent, and the crew's model left alone."""
        herdr = self.switch(self.reader(screens), target, refuses=message)
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-haiku-4-5")
        return [call.args[3:] for call in herdr.call_args_list if call.args[1] == "send-keys"]

    def test_focus_that_never_reaches_the_row_presses_nothing(self):
        """The live mis-press: one move off the row in use landed on Fable 5."""
        self.crew("claude")
        keys = self.refuses(self.claude_screens([]), "mid", "kept focus on its 'opus 5' row")
        self.assertEqual(keys, [("up",)])

    def test_focus_wandering_onto_another_row_presses_nothing(self):
        self.crew("claude")
        # Fable 5, the row one below the one in use, is never the row mid asked for.
        # Focus keeps moving but never onto row 6, so the step cap ends it.
        wandering = [claude_picker(row) for row in (8, 9, 10, 1, 2, 3, 4, 5, 7) * 4]
        keys = self.refuses(["", *wandering], "mid", "never focused a 'sonnet 5' row")
        self.assertNotIn(("s",), keys)

    def test_a_picker_with_no_focused_row_presses_nothing(self):
        self.crew("claude")
        keys = self.refuses(["", claude_picker(None)], "mid", "no focused row")
        self.assertEqual(keys, [])

    def test_a_picker_without_the_session_only_key_switches_nothing(self):
        self.crew("claude")
        kept = claude_picker(6, footer="Enter to set as default · Esc to cancel")
        keys = self.refuses(["", claude_picker(CLAUDE_IN_USE), kept], "mid", "no session-only")
        self.assertEqual(keys, [("up",)])

    def test_the_confirm_modal_naming_the_asked_model_is_answered_with_enter(self):
        self.crew("claude")
        herdr = self.switch(
            self.reader(
                self.claude_screens(
                    [6],
                    "Set model to Sonnet 5 for this session only",
                    modal=claude_modal("Sonnet 5"),
                )
            ),
            "mid",
        )
        keys = [call.args[3:] for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(keys, [("up",), ("s",), ("enter",)])
        self.assertIn("Jack switched to claude-sonnet-5.", self.output.getvalue())
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-sonnet-5")

    def test_a_confirm_modal_for_another_model_is_never_answered(self):
        """The modal opens on whatever model the picker reached, and it is not the one asked."""
        self.crew("claude")
        keys = self.refuses(
            self.claude_screens([6], modal=claude_modal("Fable 5")),
            "mid",
            "asks to switch to Fable 5, not 'sonnet 5'",
        )
        self.assertNotIn(("enter",), keys)

    def test_a_confirm_modal_whose_yes_is_not_focused_is_never_answered(self):
        self.crew("claude")
        keys = self.refuses(
            self.claude_screens([6], modal=claude_modal("Sonnet 5", yes_focused=False)),
            "mid",
            "Yes option unfocused",
        )
        self.assertNotIn(("enter",), keys)

    def test_an_immediate_session_only_confirmation_needs_no_modal(self):
        """`s` can apply the switch with no confirm: its own line is then the proof."""
        self.crew("claude")
        herdr = self.switch(
            self.reader(self.claude_screens([6], "⎿  Set model to Sonnet 5 for this session only")),
            "mid",
        )
        keys = [call.args[3:] for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(keys, [("up",), ("s",)])
        self.assertIn("Jack switched to claude-sonnet-5.", self.output.getvalue())

    def test_a_proven_switch_records_the_model_for_the_crew(self):
        self.crew("claude")
        self.switch(
            self.reader(self.claude_screens([6], "⎿  Set model to Sonnet 5 for this session only")),
            "mid",
        )
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-sonnet-5")

    def test_a_picker_still_painting_its_focus_is_waited_for(self):
        """The live race: the picker had its focus one paint late, not missing for good."""
        self.crew("claude")
        api, state = self.picker_api(
            lambda focused: claude_picker(CLAUDE_IN_USE if focused else None),
            "⎿  Set model to Opus 5 for this session only",
            unfocused_reads=2,
        )
        with patch.object(runtime, "herdr", side_effect=api):
            args = cli.parser().parse_args(
                ["--session", self.meta["id"], "model", "Jack", "strong"]
            )
            agents.switch_model(args, self.pane, self.project)
        self.assertEqual(state["pressed"], [("s",)])
        self.assertIn("Jack switched to claude-opus-5.", self.output.getvalue())
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-opus-5")

    def test_a_session_only_line_from_an_earlier_switch_never_confirms_this_one(self):
        """The picker has scrolled out of the tail, and the old line is all that remains."""
        self.crew("claude")
        stale = "⎿  Set model to Opus 5 for this session only"
        api, state = self.picker_api(
            lambda _: "\n".join([stale, claude_picker(CLAUDE_IN_USE)]),
            stale,
        )
        with patch.object(runtime, "herdr", side_effect=api):
            args = cli.parser().parse_args(
                ["--session", self.meta["id"], "model", "Jack", "strong"]
            )
            with self.assertRaisesRegex(CaptainError, "did not confirm"):
                agents.switch_model(args, self.pane, self.project)
        self.assertEqual(state["pressed"], [("s",)])
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-haiku-4-5")

    def test_a_confirmation_above_the_open_picker_is_an_earlier_switch(self):
        """A session-only line left in the tail by an earlier switch proves nothing now."""
        self.crew("claude")
        stale = "⎿  Set model to Sonnet 5 for this session only\n" + claude_picker(6)
        keys = self.refuses(
            ["", claude_picker(CLAUDE_IN_USE), claude_picker(6), stale], "mid", "did not confirm"
        )
        self.assertEqual(keys, [("up",), ("s",)])

    def test_a_confirmation_saving_the_default_after_the_modal_is_refused(self):
        """The confirm step can persist the default: a line saying so is never a switch."""
        self.crew("claude")
        saved = "⎿  Set model to Sonnet 5 and saved as your default for new sessions"
        keys = self.refuses(
            self.claude_screens([6], saved, modal=claude_modal("Sonnet 5")),
            "mid",
            "did not confirm",
        )
        self.assertEqual(keys, [("up",), ("s",), ("enter",)])

    def test_a_claude_switch_saved_as_the_users_default_is_not_accepted(self):
        self.crew("claude")
        saved = "⎿  Set model to Sonnet 5 and saved as your default for new sessions"
        self.refuses(self.claude_screens([6], saved), "mid", "did not confirm")

    def test_a_switch_is_recorded_in_session_memory(self):
        self.crew("claude")
        self.switch(
            self.reader(self.claude_screens([6], "Set model to Sonnet 5 for this session only")),
            "mid",
        )
        graph = store.read_json(self.directory / "graph.json")
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        recorded = [
            (labels[link["source"]], link["relation"], labels[link["target"]])
            for link in graph["links"]
        ]
        self.assertIn(("Jack", "model", "claude-sonnet-5"), recorded)

    def test_codex_switch_picks_the_numbered_row_and_keeps_the_default_effort(self):
        agent_name = self.crew("codex", "gpt-6-astra")
        herdr = self.switch(
            self.reader(["", CODEX_PICKER, CODEX_EFFORT, "• Model changed to gpt-5.6-sol medium"]),
            "mid",
        )
        keys = [call.args for call in herdr.call_args_list if call.args[1] == "send-keys"]
        self.assertEqual(
            keys,
            [("agent", "send-keys", agent_name, "2"), ("agent", "send-keys", agent_name, "enter")],
        )
        self.assertIn("Jack switched to gpt-5.6-sol.", self.output.getvalue())

    def test_a_pane_that_never_confirms_reports_an_error_and_records_nothing(self):
        self.crew("claude")
        with self.assertRaises(CaptainError) as error:
            self.switch(self.reader(["still working"]), "cheap")
        self.assertIn("delivery is unknown", str(error.exception))
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "claude-haiku-4-5")

    def test_a_confirmation_naming_another_model_is_not_accepted(self):
        self.crew("claude")
        wrong = "Set model to Haiku 4.5 for this session only"
        self.refuses(self.claude_screens([6], wrong), "mid", "did not confirm")

    def test_codex_reports_a_model_its_picker_does_not_offer(self):
        self.crew("codex", "gpt-6-astra")
        # A screen from a Codex build whose picker dropped a row we still list.
        stale_picker = "\n".join(
            line for line in CODEX_PICKER.splitlines() if "gpt-5.5" not in line
        )
        with self.assertRaises(CaptainError) as error:
            self.switch(self.reader(["", stale_picker]), "gpt-5.5")
        self.assertIn("/model picker", str(error.exception))

    def pi_catalog(self):
        return patch.object(
            models, "pi_models", return_value=(("openai-codex/gpt-5.5", ("gpt-5.5",)),)
        )

    def test_pi_switch_sends_the_exact_id_and_needs_no_picker_or_enter(self):
        with self.pi_catalog():
            agent_name = self.crew("pi", "anthropic/claude-sonnet-5")
            herdr = self.switch(
                self.reader(["thinking…", "Model: openai-codex/gpt-5.5"]), "gpt-5.5"
            )
        calls = [call.args for call in herdr.call_args_list]
        self.assertIn(("agent", "prompt", agent_name, "/model openai-codex/gpt-5.5"), calls)
        self.assertEqual([call for call in calls if call[1] == "send-keys"], [])
        self.assertIn("Jack switched to openai-codex/gpt-5.5.", self.output.getvalue())

    def test_pis_status_footer_naming_the_model_is_not_a_confirmation(self):
        footer = "↑3.8k ↓5 $0.019 (sub) 1.4%/272k (auto)   (openai-codex) gpt-5.5 • medium"
        with self.pi_catalog():
            self.crew("pi", "anthropic/claude-sonnet-5")
            with self.assertRaises(CaptainError) as error:
                self.switch(self.reader([footer]), "gpt-5.5")
        self.assertIn("did not confirm", str(error.exception))

    def grok_composer(self):
        """Grok's boxed, empty composer, the only shape a switch may type into."""
        return "\n".join(
            [
                "╭" + "─" * 46 + "╮",
                "│ ❯" + " " * 44 + "│",
                "╰" + "─" * 22 + " Grok 4.6 (high) ─────╯",
            ]
        )

    def test_grok_switch_sends_the_exact_id_and_takes_its_switched_to_line(self):
        agent_name = self.crew("grok", "grok-4.6")
        herdr = self.switch(
            self.reader(["thinking…", "Switched to Grok 4.7"], composer=self.grok_composer()),
            "strong",
        )
        calls = [call.args for call in herdr.call_args_list]
        self.assertIn(("agent", "prompt", agent_name, "/model grok-4.7"), calls)
        self.assertEqual([call for call in calls if call[1] == "send-keys"], [])
        self.assertIn("Jack switched to grok-4.7.", self.output.getvalue())
        record = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(record["model"], "grok-4.7")

    def test_a_grok_draft_in_the_box_never_receives_model_input(self):
        self.crew("grok", "grok-4.6")
        drafted = self.grok_composer().replace("│ ❯ ", "│ ❯ user draft ")
        api = self.reader([], composer=drafted)
        with patch.object(runtime, "herdr", side_effect=api) as calls:
            args = cli.parser().parse_args(["--session", self.meta["id"], "model", "Jack", "cheap"])
            with self.assertRaisesRegex(CaptainError, "empty composer"):
                agents.switch_model(args, self.pane, self.project)
        self.assertFalse(any(c.args[1] in ("prompt", "send-keys") for c in calls.call_args_list))

    def test_an_unknown_crew_name_reports_the_available_crew(self):
        self.crew("claude")
        args = cli.parser().parse_args(["--session", self.meta["id"], "model", "Davy", "mid"])
        with self.assertRaises(CaptainError) as error:
            agents.switch_model(args, self.pane, self.project)
        self.assertIn("Jack", str(error.exception))

    def test_blocked_unknown_or_nonempty_composer_never_gets_model_input(self):
        self.crew("claude")
        for status, composer in (
            ("blocked", "❯"),
            (None, "❯"),
            ("idle", "❯ user draft"),
            ("idle", "unreadable"),
            ("idle", CODEX_PICKER),
        ):
            with self.subTest(status=status, composer=composer):
                api = self.reader([], status=status, composer=composer)
                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    args = cli.parser().parse_args(
                        ["--session", self.meta["id"], "model", "Jack", "mid"]
                    )
                    with self.assertRaisesRegex(CaptainError, "empty composer"):
                        agents.switch_model(args, self.pane, self.project)
                self.assertFalse(
                    any(c.args[1] in ("prompt", "send-keys") for c in calls.call_args_list)
                )

    def test_claude_enter_requires_the_exact_model_command_draft(self):
        agent = self.crew("claude")
        for draft, succeeds in (("❯ /model", True), ("❯ user draft", False)):
            with self.subTest(draft=draft):
                api = self.reader(
                    [
                        "❯",
                        draft,
                        claude_picker(CLAUDE_IN_USE),
                        claude_picker(6),
                        "Set model to Sonnet 5 for this session only",
                    ]
                )
                with patch.object(runtime, "herdr", side_effect=api) as calls:
                    args = cli.parser().parse_args(
                        ["--session", self.meta["id"], "model", "Jack", "mid"]
                    )
                    if succeeds:
                        agents.switch_model(args, self.pane, self.project)
                    else:
                        with self.assertRaisesRegex(CaptainError, "delivery is unknown"):
                            agents.switch_model(args, self.pane, self.project)
                keys = [c.args[3:] for c in calls.call_args_list if c.args[1] == "send-keys"]
                self.assertEqual(keys, [("enter",), ("up",), ("s",)] if succeeds else [])
                self.assertTrue(all(c.args[2] == agent for c in calls.call_args_list[1:]))


class ClaudePickerReadingTests(unittest.TestCase):
    """What the pane says about Claude Code's /model picker, which decides every keypress."""

    def lines(self, focused=CLAUDE_IN_USE, **picker):
        screen = claude_picker(focused, **picker)
        return [line.strip() for line in screen.splitlines() if line.strip()]

    def test_a_row_reads_as_its_label_and_only_the_focus_marker_is_focus(self):
        rows = panes.claude_picker_rows(self.lines())
        # Row 1 describes itself as "Opus 5 ·…", which is description and never a label.
        self.assertEqual(rows[0], (1, "default (recommended)", False))
        self.assertEqual(rows[1], (2, "opus 5.5", False))
        # The in-use mark is not part of the label, and the scroll arrow is not focus.
        self.assertEqual([row for row in rows if row[2]], [(CLAUDE_IN_USE, "opus 5", True)])
        self.assertEqual(rows[-1], (10, "opus 4.7", False))
        self.assertIsNone(panes.claude_picker_focused(panes.claude_picker_rows(self.lines(None))))

    def test_the_steps_allowed_count_the_rows_held_out_of_view(self):
        self.assertEqual(panes.claude_picker_steps(self.lines()), 2 * (10 + 2))
        self.assertEqual(panes.claude_picker_steps(self.lines(hidden=0)), 2 * 10)

    def test_the_move_goes_towards_the_wanted_row_and_down_for_one_out_of_view(self):
        rows = panes.claude_picker_rows(self.lines())
        focused = panes.claude_picker_focused(rows)
        for label, key in (("sonnet 5", "up"), ("fable 5", "down"), ("opus 4.6", "down")):
            with self.subTest(label=label):
                self.assertEqual(panes.claude_picker_key(rows, label, focused), key)


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


class CrewModelTests(SessionCase):
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

    EMPTY_COMPOSER = "› Ask Codex to do anything"

    def test_crew_model_is_resolved_passed_to_the_native_cli_and_recorded(self):
        for provider, name, text, model, flag in (
            ("claude", "jack", "Opus", "claude-opus-5", "--model"),
            ("codex", "gibbs", "5.6 terra", "gpt-5.6-terra", "-m"),
            ("pi", "will", "cheap", "ollama/llama3.2:3b", "--model"),
        ):
            with self.subTest(provider=provider):
                args = self.args(
                    "crew",
                    name,
                    "--agent",
                    provider,
                    "--task",
                    "build",
                    "--placement",
                    "tab",
                    "--model",
                    text,
                )
                created = {
                    "pane": {"pane_id": "w1:p2", "agent": provider, "agent_status": "idle"},
                    "root_pane": {"pane_id": "w1:p3"},
                    "tab_id": "w1:t9",
                    "agent": {"name": f"c-{self.meta['id'][:8]}-{name}", "agent_status": "working"},
                }
                with (
                    patch.object(
                        runtime,
                        "herdr",
                        side_effect=pane_stub(
                            lambda *call, **_: (
                                (
                                    "────────\n\n────────\n/tmp/project\n0.1%/200k"
                                    if provider == "pi"
                                    else self.EMPTY_COMPOSER
                                )
                                if call[:2] == ("agent", "read")
                                else created
                            )
                        ),
                    ),
                    patch.object(agents, "executable", return_value=f"/bin/{provider}"),
                    patch.object(
                        models,
                        "pi_models",
                        return_value=(("ollama/llama3.2:3b", ()),),
                    ),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()) as errors,
                ):
                    agents.create_crew(args, self.pane, self.project)
                self.assertEqual(json.loads(output.getvalue())["model"], model)
                self.assertEqual(errors.getvalue(), f"Model: {model} (from {text!r})\n")
                launcher = shlex.split((self.directory / f"crew-{name}.sh").read_text())
                self.assertEqual(launcher[launcher.index(flag) + 1], model)
                self.assertEqual(launcher.count(flag), 1)
                saved = store.read_json(self.directory / "session.json")["crew"][name]
                self.assertEqual(saved["model"], model)
                graph = store.read_json(self.directory / "graph.json")
                self.assertIn(model, [node["label"] for node in graph["nodes"]])

    def test_crew_without_a_model_recruits_cheap_instead_of_the_native_default(self):
        args = self.args("crew", "--agent", "claude", "--task", "build", "--placement", "tab")
        # The parser leaves it unset; create_crew is what reads [crew] model.
        self.assertIsNone(args.model)
        created = {
            "pane": {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
            "root_pane": {"pane_id": "w1:p3"},
            "tab_id": "w1:t9",
            "agent": {"name": f"c-{self.meta['id'][:8]}-jack", "agent_status": "working"},
        }
        with (
            patch.object(
                runtime,
                "herdr",
                side_effect=pane_stub(
                    lambda *call, **_: (
                        self.EMPTY_COMPOSER if call[:2] == ("agent", "read") else created
                    )
                ),
            ),
            patch.object(agents, "executable", return_value="/bin/claude"),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            agents.create_crew(args, self.pane, self.project)
        self.assertEqual(json.loads(output.getvalue())["model"], "claude-haiku-4-5")
        self.assertEqual(errors.getvalue(), "Model: claude-haiku-4-5 (from 'cheap')\n")
        launcher = shlex.split((self.directory / "crew-jack.sh").read_text())
        self.assertEqual(launcher[launcher.index("--model") + 1], "claude-haiku-4-5")

    def test_the_cheap_default_never_silently_reaches_a_mid_or_strong_model(self):
        """The whole point of the default: an unspecified tier cannot cost mid/strong money."""
        for provider, cheap in (("claude", "claude-haiku-4-5"), ("codex", "gpt-5.6-luna")):
            with self.subTest(provider=provider):
                args = self.args("crew", "--agent", provider, "--task", "commit the fix")
                self.assertIsNone(args.model)
                self.assertEqual(
                    models.resolve_model(provider, config.text("crew", "model")), cheap
                )
                tiers = models.tiers_for(provider)
                self.assertEqual(tiers["cheap"], cheap)
                self.assertNotIn(cheap, (tiers["mid"], tiers["strong"]))

    def test_unmatched_or_ambiguous_model_lists_options_and_creates_nothing(self):
        for provider, text, message in (
            (
                "claude",
                "zzz",
                "No claude model matches 'zzz'. Tiers: cheap, mid, strong. "
                "Options: claude-haiku-4-5, ",
            ),
            ("codex", "gpt-5.6", "ambiguous for codex: gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol"),
            ("codex", " ", "Provide a tier (cheap|mid|strong) or model name. codex models:"),
        ):
            with self.subTest(text=text):
                args = self.args(
                    "crew",
                    "--agent",
                    provider,
                    "--task",
                    "build",
                    "--placement",
                    "tab",
                    "--model",
                    text,
                )
                with patch.object(runtime, "herdr") as api:
                    with self.assertRaises(runtime.CaptainError) as error:
                        agents.create_crew(args, self.pane, self.project)
                self.assertIn(message, str(error.exception))
                api.assert_not_called()
                self.assertEqual(store.read_json(self.directory / "session.json")["crew"], {})


if __name__ == "__main__":
    unittest.main()
