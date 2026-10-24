"""Omitted recruitment inputs resolve from flags, settings and captain.json, never a prompt."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import config, runtime
from captain_barbossa.placement import Placement
from captain_barbossa.runtime import CaptainError
from captain_barbossa.sessions import Session


class RecruitmentDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.pane = {"workspace_id": "w1", "tab_id": "captain", "pane_id": "captain"}
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.directory / "captain.json").write_text(json.dumps({"provider": "codex"}))
        self.settings = {}
        self.enterContext(
            patch.object(config, "settings", lambda: config.merge(config.defaults(), self.settings))
        )
        self.enterContext(patch.object(config, "source", return_value=None))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.herdr = self.enterContext(patch.object(runtime, "herdr", side_effect=self.answer))

    def answer(self, *call, **_):
        if call[:2] == ("pane", "resize"):
            return {"resize": {"changed": True}}
        if call[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": "captain", "number": 1}, {"tab_id": "t2", "number": 2}]}
        if call[:2] == ("pane", "list"):
            return {"panes": [self.pane, {"pane_id": "p9", "tab_id": "t2"}]}
        raise AssertionError(f"resolution asked Herdr: {call}")

    def resolve(self, **flags):
        args = SimpleNamespace(
            crew_agent=None, placement=None, direction=None, split_pane=None, model=None
        )
        vars(args).update(flags)
        placement = Placement(self.pane, Session(self.directory, {"crew": {}}))
        return placement.resolve(args, self.directory)

    def test_no_flags_take_the_captains_cli_the_crew_tier_and_the_next_slot(self):
        recruit = self.resolve()
        self.assertEqual(recruit.agent, "codex")
        self.assertEqual(recruit.model, config.defaults()["models"]["codex"]["cheap"])
        self.assertEqual((recruit.placement, recruit.direction), ("pane", "vertical"))
        self.assertEqual((recruit.spot.pane, recruit.spot.column), ("captain", 2))
        self.assertEqual(recruit.capabilities, ("hooks",))
        self.assertIn("captain.json", recruit.sources["agent"])
        self.assertIn("[crew] model = 'cheap'", recruit.sources["model"])
        self.assertEqual(recruit.sources["placement"], "[crew] placement from defaults.toml")

    def test_crew_agent_and_placement_are_shipped_defaults(self):
        self.assertEqual(config.defaults()["crew"]["agent"], "")
        self.assertEqual(config.defaults()["crew"]["placement"], "auto")
        self.assertIn('# placement = "auto"', config.template())

    def test_settings_beat_captain_json_and_flags_beat_settings(self):
        self.settings = {"crew": {"agent": "claude", "model": "mid", "placement": "tab"}}
        recruit = self.resolve()
        self.assertEqual((recruit.agent, recruit.placement), ("claude", "tab"))
        self.assertEqual(recruit.model, config.defaults()["models"]["claude"]["mid"])
        self.assertEqual(recruit.capabilities, ("hooks", "hook_delivered_mail"))
        self.herdr.assert_not_called()
        recruit = self.resolve(crew_agent="grok", model="strong", placement="pane")
        self.assertEqual((recruit.agent, recruit.placement), ("grok", "pane"))
        self.assertEqual(recruit.model, config.defaults()["models"]["grok"]["strong"])
        self.assertEqual(recruit.sources["model"], "--model strong")

    def test_a_named_pane_without_a_direction_splits_beside_it_unasked(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            recruit = self.resolve(split_pane="p9")
        self.assertEqual((recruit.spot.pane, recruit.spot.tab), ("p9", "t2"))
        self.assertEqual((recruit.placement, recruit.direction), ("pane", "vertical"))
        with self.assertRaisesRegex(CaptainError, "not in this workspace.*Omit --split-pane"):
            self.resolve(split_pane="p7")

    def test_only_an_unanswerable_agent_asks_and_names_the_setting(self):
        (self.directory / "captain.json").unlink()
        with (
            patch.object(sys.stdin, "isatty", return_value=False),
            self.assertRaisesRegex(CaptainError, r"Ask the user.*\[crew\] agent"),
        ):
            self.resolve()

    def test_bad_settings_and_models_fail_before_herdr_moves_a_pane(self):
        for settings, pattern in (
            ({"crew": {"agent": "gpt"}}, "agent is 'gpt'"),
            ({"crew": {"placement": "window"}}, "placement is 'window'"),
            ({"crew": {"model": "nonesuch"}}, "No codex model"),
        ):
            self.settings = settings
            with self.subTest(settings=settings), self.assertRaisesRegex(CaptainError, pattern):
                self.resolve()
        self.herdr.assert_not_called()
        self.settings = {}
        with self.assertRaisesRegex(CaptainError, "apply only to --placement pane"):
            self.resolve(placement="tab", direction="vertical")


if __name__ == "__main__":
    unittest.main()
