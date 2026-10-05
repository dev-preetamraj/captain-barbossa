"""A hook command baked into a session before 0.30.0 still resolves after the upgrade."""

import subprocess
import sys
import unittest

from captain_barbossa import events, memory


class LegacyHookImportTest(unittest.TestCase):
    def test_memory_still_exports_append_event(self):
        self.assertIs(memory.append_event, events.append_event)

    def test_baked_hook_command_runs(self):
        result = subprocess.run(
            [sys.executable, "-c", "from captain_barbossa.memory import append_event"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
