import tempfile
import unittest
from pathlib import Path

from captain_barbossa import instructions as instruction_prompts


class InstructionSizeTests(unittest.TestCase):
    """Regression tests for the generated instruction sizes (instruction_prompts.agent_instructions)."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_crew_instructions_are_much_shorter_than_the_captains(self):
        crew_text = instruction_prompts.agent_instructions(self.directory, "crew member Jack")
        captain_text = instruction_prompts.agent_instructions(self.directory, "Captain Barbossa")
        crew_words = len(crew_text.split())
        captain_words = len(captain_text.split())
        self.assertLess(crew_words, captain_words)
        # Guards against the crew memory block creeping back toward the captain's.
        self.assertLessEqual(crew_words, 320)

    def test_only_the_captain_learns_to_retier_a_running_crew(self):
        captain = " ".join(
            instruction_prompts.agent_instructions(self.directory, "Captain Barbossa").split()
        )
        self.assertIn("CAPTAIN model 'NAME' cheap|mid|strong|<model>", captain)
        self.assertIn(
            "a cheap crew that is stuck, looping, or out of its depth -> step up; "
            "a mechanical follow-up on a strong crew -> step down",
            captain,
        )
        crew = instruction_prompts.agent_instructions(self.directory, "crew member Jack")
        self.assertNotIn("CAPTAIN model", crew)

    def test_routine_work_is_pinned_to_cheap_and_never_steps_up_on_a_hunch(self):
        captain = " ".join(
            instruction_prompts.agent_instructions(self.directory, "Captain Barbossa").split()
        )
        cheap_sentence = captain.split("cheap for mechanical work")[1].split("mid for a")[0]
        for routine in ("commits", "tests", "lint", "formatting", "docs", "chores"):
            self.assertIn(routine, cheap_sentence)
        self.assertIn("Never step up just because a task feels risky or important.", captain)
        # The old wording made ambiguity a reason to spend more, which drifted every task up.
        self.assertNotIn("Step up a tier when the task is ambiguous", captain)
        self.assertNotIn("step up only when the user asks for a stronger model", captain)

    def test_both_roles_preserve_existing_files_and_ask_before_replacing_content(self):
        rule = (
            "Never overwrite, rewrite from scratch, or discard existing files or "
            "unsaved/uncommitted work (yours or anyone else's). Edit in place; preserve "
            "existing content. If an assignment implies replacing existing content, "
            "stop and ask the user first; never decide alone."
        )
        for role in ("Captain Barbossa", "crew member Jack"):
            with self.subTest(role=role):
                instructions = " ".join(
                    instruction_prompts.agent_instructions(self.directory, role).split()
                )
                self.assertIn(rule, instructions)
                captain_rule = "The captain must also ask the user first, never instruct crew to override files."
                self.assertEqual(captain_rule in instructions, role == "Captain Barbossa")

    def test_crew_only_gets_the_report_command_for_every_provider(self):
        for provider in (None, "codex", "claude", "pi"):
            with self.subTest(provider=provider):
                crew = instruction_prompts.agent_instructions(
                    self.directory, "crew member Jack", provider
                )
                self.assertEqual(len([line for line in crew.splitlines() if " memory " in line]), 2)
                self.assertIn("memory add Jack report '<summary>'", crew)
                self.assertIn("memory show --scope repo", crew)
                for phrase in (
                    "CAPTAIN",
                    "Do not create Herdr panes/tabs",
                    "Only the captain manages crew",
                    "Read project/session memory",
                    "context compaction",
                    "Crew recruiting ruleset",
                    "Any task request",
                    "captain_wait",
                    "--model",
                ):
                    self.assertNotIn(phrase, crew)

    def test_captain_instructions_keep_the_full_memory_ruleset(self):
        captain_text = instruction_prompts.agent_instructions(self.directory, "Captain Barbossa")
        self.assertIn("Do not store secrets", captain_text)
        self.assertIn("Commit and PR attribution", captain_text)

    def test_a_quiet_wait_result_is_rearmed_without_spending_a_captain_turn(self):
        """Every delivery wakes the captain; the rule decides whether that costs a turn."""
        text = instruction_prompts.agent_instructions(self.directory, "Captain Barbossa", "pi")
        block = text.split("it falls back to the pane tail. ")[1].split(" For more detail")[0]
        self.assertIn("rearm at once, silently", block)
        self.assertIn("Act only on a new report, a newly blocked crew, or an error", block)
        # The old rule made every delivery, timeout included, a full working turn.
        self.assertNotIn("Rearm after a timeout if work remains", block)
        # Token-budgeted: rewording the rule must not buy itself more lines.
        self.assertLessEqual(len(block.splitlines()), 7)

    def test_captain_self_checks_before_doing_the_task_directly(self):
        captain = " ".join(
            instruction_prompts.agent_instructions(self.directory, "Captain Barbossa").split()
        )
        self.assertIn(
            "Before any edit, file write, build, test, or debug step, recruit crew and "
            "assign it; never do it yourself.",
            captain,
        )
        self.assertIn(
            "Self-check first: about to edit a file, write output, or run a "
            "build/test/debug step yourself? Stop, recruit crew instead.",
            captain,
        )
        self.assertIn(
            'Work directly only if the user explicitly says "yourself", "no crew", or '
            '"do not recruit".',
            captain,
        )
        crew = instruction_prompts.agent_instructions(self.directory, "crew member Jack")
        self.assertNotIn("Self-check first", crew)

    def test_graphify_query_is_not_advertised_as_always_available(self):
        text = instruction_prompts.agent_instructions(self.directory, "Captain Barbossa")
        query_line = next(line for line in text.splitlines() if "memory query" in line)
        self.assertIn("if Graphify is installed", query_line)


if __name__ == "__main__":
    unittest.main()
