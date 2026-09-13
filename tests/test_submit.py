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
    a usage-limit notice over the composer does.
    """

    def __init__(self, status="working", swallows_enter=False):
        self.status = status
        self.draft = True
        self.swallows_enter = swallows_enter
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
        composer = f"› {TASK}" if self.draft else "› Ask Codex to do anything"
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

    def test_a_working_codex_with_an_empty_composer_lands_without_enter(self):
        pane = CodexPane()
        pane.draft = False
        self.submit(pane)
        self.assertNotIn(("agent", "send-keys"), pane.verbs())


if __name__ == "__main__":
    unittest.main()
