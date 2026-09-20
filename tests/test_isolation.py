"""Guard: the suite must not be able to reach the developer's real HOME or memory roots.

This module deliberately does not import tests/home_isolation.py. Something else has to
have imported it (tests/test_captain.py does), so these tests fail if that import is
ever dropped and the suite starts writing into the real captain memory root again.
"""

import os
import pwd
import tempfile
import unittest
from pathlib import Path

from captain_barbossa import config, memory


def real_home():
    """Where Path.home() would land without the isolation, straight from the passwd entry."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def real_temp_root():
    """memory.temp_root()'s default, which a live captain session also exports."""
    return Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}"


class IsolationTests(unittest.TestCase):
    def test_no_live_captain_session_variable_survives_into_the_suite(self):
        for name in (
            "CAPTAIN_STATE_ROOT",
            "CAPTAIN_TEMP_ROOT",
            "CAPTAIN_SESSION",
            "CAPTAIN_PROJECT",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, os.environ)

    def test_home_points_somewhere_other_than_the_account_running_the_suite(self):
        self.assertTrue(Path.home().is_dir())
        self.assertNotEqual(Path.home(), real_home())
        self.assertFalse((Path.home() / config.SETTINGS_PATH).exists())

    def test_settings_fall_through_to_the_shipped_defaults(self):
        # An uncommented [placement] shape in a real home file used to decide what the
        # placement tests saw, so a deliberate user setting turned the gate red.
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        for name in ("captain_tab", "crew_tab"):
            with self.subTest(name=name):
                self.assertEqual(
                    config.lookup("placement", name), config.defaults()["placement"][name]
                )

    def test_the_default_memory_roots_are_not_the_real_ones(self):
        self.assertNotEqual(memory.temp_root(), real_temp_root())
        self.assertNotEqual(memory.state_root(), real_home() / ".local/state/captain-barbossa")
        for root in (memory.temp_root(), memory.state_root()):
            with self.subTest(root=root):
                self.assertFalse(root.is_relative_to(real_home()))

    def test_a_session_created_with_no_root_set_lands_outside_the_real_roots(self):
        """The end state that matters: a forgetful test must not litter the real root."""
        project = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        directory, _ = memory.session(
            project, {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}, create=True
        )
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_relative_to(real_temp_root()))
        self.assertFalse(directory.is_relative_to(real_home()))


if __name__ == "__main__":
    unittest.main()
