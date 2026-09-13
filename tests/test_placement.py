import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, layout
from captain_barbossa.runtime import CaptainError


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.pane = {"workspace_id": "w1", "tab_id": "captain", "pane_id": "captain"}
        self.tabs = {"captain": {"captain": (0, 0, 480, 120)}}
        self.meta = {"crew": {}}
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.api = self.enterContext(patch.object(agents, "herdr", side_effect=self.herdr))

    def herdr(self, *call):
        self.assertEqual(call[:3], ("pane", "layout", "--pane"))
        for geometry in self.tabs.values():
            if call[3] in geometry:
                return {
                    "layout": {
                        "panes": [
                            {
                                "pane_id": pane,
                                "rect": dict(zip(("x", "y", "width", "height"), rect)),
                            }
                            for pane, rect in geometry.items()
                        ]
                    }
                }
        raise CaptainError("pane closed")

    def choose(self, direction=None, target="auto", placement="pane"):
        args = SimpleNamespace(direction=direction, split_pane=target)
        crew_panes = {
            crew["pane"] for crew in self.meta["crew"].values() if crew["status"] != "dismissed"
        }
        return agents.choose_split(args, self.pane, placement, crew_panes, self.meta)

    def recruit(self, direction=None):
        chosen, target, tab, reason = self.choose(direction)
        name = f"crew{len(self.meta['crew']) + 1}"
        if target is None:
            tab = name
            self.tabs[tab] = {name: (0, 0, 480, 120)}
        else:
            x, y, _, _ = self.tabs[tab][target]
            width, height = layout.half(self.tabs[tab][target], chosen)
            self.tabs[tab][target] = (x, y, width, height)
            self.tabs[tab][name] = (
                x + width if chosen == "vertical" else x,
                y + height if chosen == "horizontal" else y,
                width,
                height,
            )
        self.meta["crew"][name] = {"pane": name, "tab": tab, "status": "started"}
        return chosen, target, tab, reason

    def test_recruits_fill_captain_then_four_pane_crew_tabs(self):
        expected = [
            ("vertical", "captain", "captain"),
            ("horizontal", "crew1", "captain"),
            (None, None, "crew3"),
            ("vertical", "crew3", "crew3"),
            ("horizontal", "crew3", "crew3"),
            ("horizontal", "crew4", "crew3"),
            (None, None, "crew7"),
            ("vertical", "crew7", "crew7"),
            ("horizontal", "crew7", "crew7"),
            ("horizontal", "crew8", "crew7"),
            (None, None, "crew11"),
        ]
        for choice in expected:
            with self.subTest(choice=choice):
                self.assertEqual(self.recruit()[:3], choice)
                self.assertLessEqual(len(self.tabs["captain"]), 3)
                self.assertEqual(self.tabs["captain"]["captain"], (0, 0, 240, 120))
                self.assertTrue(all(len(panes) <= 4 for panes in self.tabs.values()))
        self.assertEqual(self.tabs["captain"]["crew1"], (240, 0, 240, 60))
        self.assertEqual(self.tabs["captain"]["crew2"], (240, 60, 240, 60))

    def test_first_crew_tab_split_is_vertical_even_when_horizontal_scores_better(self):
        for _ in range(3):
            self.recruit()
        self.tabs["crew3"]["crew3"] = (0, 0, 180, 120)
        self.assertEqual(self.choose()[:3], ("vertical", "crew3", "crew3"))

    def test_small_captain_tab_opens_another_tab_without_shrinking_the_captain(self):
        self.tabs["captain"]["captain"] = (0, 0, 100, 120)
        self.assertEqual(self.choose()[:2], (None, None))
        self.tabs["captain"]["captain"] = (0, 0, 480, 40)
        self.recruit()
        self.tabs["captain"]["crew1"] = (240, 0, 240, 20)
        self.assertEqual(self.choose()[:2], (None, None))
        self.assertEqual(self.tabs["captain"]["captain"], (0, 0, 240, 40))

    def test_reuses_space_in_earlier_crew_tab(self):
        for _ in range(7):
            self.recruit()
        self.meta["crew"]["crew6"]["status"] = "dismissed"
        del self.tabs["crew3"]["crew6"]
        self.tabs["crew3"]["crew4"] = (240, 0, 240, 120)
        self.assertEqual(self.choose()[:3], ("horizontal", "crew4", "crew3"))

    def test_skips_mixed_and_closed_crew_tabs(self):
        for _ in range(7):
            self.recruit()
        self.tabs["crew3"] = {"crew3": (0, 0, 240, 120), "editor": (240, 0, 240, 120)}
        self.assertEqual(self.choose()[:3], ("vertical", "crew7", "crew7"))
        del self.tabs["crew3"]
        self.assertEqual(self.choose()[:3], ("vertical", "crew7", "crew7"))

    def test_small_tabs_are_skipped_and_closed_captain_layout_is_an_error(self):
        for _ in range(3):
            self.recruit()
        self.tabs["crew3"]["crew3"] = (0, 0, 100, 20)
        self.assertEqual(self.choose()[:2], (None, None))
        del self.tabs["captain"]
        with self.assertRaisesRegex(CaptainError, "pane closed"):
            self.choose()

    def test_manual_directions_override_grid_order_but_keep_caps(self):
        self.assertEqual(self.recruit("horizontal")[:2], ("horizontal", "captain"))
        self.assertEqual(self.recruit("vertical")[:2], ("vertical", "crew1"))
        self.assertEqual(self.recruit("vertical")[:2], (None, None))
        self.assertEqual(self.recruit("horizontal")[:2], ("horizontal", "crew3"))
        self.assertEqual(self.recruit("vertical")[:2], ("vertical", "crew3"))
        self.assertEqual(self.recruit("vertical")[0], "vertical")
        self.assertEqual(self.choose("horizontal")[:2], (None, None))

    def test_explicit_pane_bypasses_caps_with_auto_or_manual_direction(self):
        for _ in range(6):
            self.recruit()
        groups = {tab: (tab, dict.fromkeys(panes, "Crew")) for tab, panes in self.tabs.items()}
        with patch.object(agents, "workspace_panes", return_value=groups):
            for target, tab in (("captain", "captain"), ("crew3", "crew3")):
                for direction in ("auto", "vertical", "horizontal"):
                    with self.subTest(target=target, direction=direction):
                        chosen, actual, actual_tab, _ = self.choose(direction, target)
                        self.assertEqual((actual, actual_tab), (target, tab))
                        self.assertEqual(chosen, "vertical" if direction == "auto" else direction)

    def test_explicit_tab_never_queries_layout(self):
        self.assertEqual(self.choose(target=None, placement="tab"), (None, None, None, None))
        self.api.assert_not_called()

    def test_recruit_selects_from_fresh_metadata_under_the_crew_lock(self):
        self.recruit()
        self.recruit()
        choose_split = agents.choose_split
        args = cli.parser().parse_args(
            [
                "crew",
                "--agent",
                "codex",
                "--task",
                "test",
                "--placement",
                "pane",
                "--split-pane",
                "auto",
            ]
        )

        def check_selection(*args):
            guard.return_value.__enter__.assert_called_once()
            self.assertEqual(choose_split(*args)[:2], (None, None))
            raise CaptainError("selection checked")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(agents, "session", return_value=(Path(root), {"crew": {}})),
            patch.object(agents, "read_json", return_value=self.meta),
            patch.object(agents, "lock") as guard,
            patch.object(agents, "choose_split", side_effect=check_selection),
            self.assertRaisesRegex(CaptainError, "selection checked"),
        ):
            agents.create_crew(args, self.pane, Path(root))


if __name__ == "__main__":
    unittest.main()
