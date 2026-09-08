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
        self.assertLessEqual(crew_words, 260)

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
