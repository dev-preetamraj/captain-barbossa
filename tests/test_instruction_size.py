import tempfile
import unittest
from pathlib import Path

from captain_barbossa import agents


class InstructionSizeTests(unittest.TestCase):
    """Regression tests for the generated instruction sizes (agents.agent_instructions)."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_crew_instructions_are_much_shorter_than_the_captains(self):
        crew_text = agents.agent_instructions(self.directory, "crew member Jack")
        captain_text = agents.agent_instructions(self.directory, "Captain Barbossa")
        crew_words = len(crew_text.split())
        captain_words = len(captain_text.split())
        self.assertLess(crew_words, captain_words)
        # Guards against the crew memory block creeping back toward the captain's.
        self.assertLessEqual(crew_words, 320)

    def test_only_the_captain_learns_to_retier_a_running_crew(self):
        captain = " ".join(agents.agent_instructions(self.directory, "Captain Barbossa").split())
        self.assertIn("CAPTAIN model 'NAME' cheap|mid|strong|<model>", captain)
        self.assertIn(
            "a cheap crew that is stuck, looping, or out of its depth -> step up; "
            "a mechanical follow-up on a strong crew -> step down",
            captain,
        )
        crew = agents.agent_instructions(self.directory, "crew member Jack")
        self.assertNotIn("CAPTAIN model", crew)

    def test_both_roles_preserve_existing_files_and_ask_before_replacing_content(self):
        rule = (
            "Never overwrite, rewrite from scratch, or discard existing files or "
            "unsaved/uncommitted work (yours or anyone else's). Edit in place; preserve "
            "existing content. If an assignment implies replacing existing content, "
            "stop and ask the user first; never decide alone. The captain must also ask "
            "the user first, never instruct crew to override files."
        )
        for role in ("Captain Barbossa", "crew member Jack"):
            with self.subTest(role=role):
                instructions = " ".join(agents.agent_instructions(self.directory, role).split())
                self.assertIn(rule, instructions)

    def test_crew_memory_block_keeps_only_the_four_essential_commands(self):
        crew_text = agents.agent_instructions(self.directory, "crew member Jack")
        self.assertIn("CAPTAIN memory show", crew_text)
        self.assertIn("CAPTAIN memory add", crew_text)
        self.assertIn("CAPTAIN memory query", crew_text)
        self.assertIn("CAPTAIN memory path", crew_text)
        # Captain-only content should not leak into the crew block.
        self.assertNotIn("Do not store secrets", crew_text)
        self.assertNotIn("Commit and PR attribution", crew_text)

    def test_captain_instructions_keep_the_full_memory_ruleset(self):
        captain_text = agents.agent_instructions(self.directory, "Captain Barbossa")
        self.assertIn("Do not store secrets", captain_text)
        self.assertIn("Commit and PR attribution", captain_text)

    def test_graphify_query_is_not_advertised_as_always_available(self):
        for role in ("crew member Jack", "Captain Barbossa"):
            text = agents.agent_instructions(self.directory, role)
            query_line = next(line for line in text.splitlines() if "memory query" in line)
            self.assertIn("if Graphify is installed", query_line)


if __name__ == "__main__":
    unittest.main()
