"""Task submission: a crew counts as started only once its composer is empty."""

import unittest
from itertools import count
from unittest.mock import patch

from captain_barbossa import pane as panes
from captain_barbossa import runtime
from captain_barbossa.pane import Pane
from captain_barbossa.runtime import CaptainError

TASK = "build the thing"


class CodexPane:
    """A Codex pane that reports working while it renames its own thread.

    Herdr sees the renaming turn as activity even though the task is still an unsent
    draft in the composer; Enter submits it unless `swallows_enter` is set, which is what
    a usage-limit notice over the composer does. `readable` off is a composer whose draft
    the pane scrape cannot make out, which is indistinguishable from an empty one.
    """

    def __init__(self, status="working", swallows_enter=False, readable=True):
        self.status = status
        self.draft = True
        self.swallows_enter = swallows_enter
        self.readable = readable
        self.calls = []

    def __call__(self, *call, **kwargs):
        self.calls.append(call)
        if call[:2] == ("agent", "read"):
            return self.text()
        if call[:2] == ("agent", "send-keys") and not self.swallows_enter:
            self.draft = False
            self.status = "working"
        if call[:2] == ("agent", "get"):
            return {"agent": {"name": "builder", "agent_status": self.status}}
        return {}

    def text(self):
        if self.draft:
            composer = f"› {TASK}" if self.readable else f"  {TASK}"
        else:
            composer = "› Ask Codex to do anything"
        return "\n".join(["• Ran uv run --locked python -m unittest -q", composer, "renaming..."])

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
        with self.assertRaisesRegex(CaptainError, "did not start working"):
            self.submit(pane)
        self.assertIn(("agent", "send-keys"), pane.verbs())

    def test_enter_starts_an_idle_codex_draft(self):
        pane = CodexPane(status="idle")
        self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 1)

    def test_enter_starts_a_drafted_task_the_renaming_turn_masked(self):
        pane = CodexPane()
        self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 1)

    def test_a_draft_the_composer_scrape_cannot_read_is_still_submitted(self):
        """A Codex composer whose draft the pane does not render readably reads as empty,
        which used to count as started and left the prompt sitting unsent."""
        pane = CodexPane(readable=False)
        self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 1)
        self.assertFalse(pane.draft)

    def test_an_already_submitted_codex_task_takes_the_extra_enter_harmlessly(self):
        pane = CodexPane()
        pane.draft = False
        self.submit(pane)
        self.assertEqual(pane.verbs().count(("agent", "prompt")), 1)
        self.assertEqual(pane.verbs().count(("agent", "send-keys")), 1)

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


if __name__ == "__main__":
    unittest.main()
