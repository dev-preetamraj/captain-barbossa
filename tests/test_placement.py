import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, layout, runtime
from captain_barbossa.memory import Session
from captain_barbossa.placement import Placement
from captain_barbossa.runtime import CaptainError


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.pane = {"workspace_id": "w1", "tab_id": "captain", "pane_id": "captain"}
        self.tabs = {"captain": {"captain": (0, 0, 480, 120)}}
        self.meta = {"crew": {}}
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.api = self.enterContext(patch.object(runtime, "herdr", side_effect=self.herdr))

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
        return Placement(self.pane, Session(None, self.meta)).choose_split(args, placement)

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

    def test_wide_screen_fits_more_than_two_crew_in_the_captain_tab(self):
        for _ in range(6):
            chosen, target, tab, _ = self.recruit()
            self.assertIsNotNone(target)
            self.assertEqual(tab, "captain")
        self.assertEqual(set(self.tabs), {"captain"})
        self.assertGreater(len(self.tabs["captain"]) - 1, 2)

    def test_captain_pane_is_never_split_down(self):
        for _ in range(15):
            chosen, target, tab, _ = self.recruit()
            if target == "captain":
                self.assertEqual(chosen, "vertical")
            self.assertEqual(self.tabs["captain"]["captain"][3], 120)

    def test_small_captain_tab_opens_another_tab_without_shrinking_the_captain(self):
        self.tabs["captain"]["captain"] = (0, 0, 100, 120)
        self.assertEqual(self.choose()[:2], (None, None))
        self.tabs["captain"] = {"captain": (0, 0, 100, 20), "crew1": (100, 0, 100, 20)}
        self.meta["crew"]["crew1"] = {"pane": "crew1", "tab": "captain", "status": "started"}
        self.assertEqual(self.choose()[:2], (None, None))
        self.assertEqual(self.tabs["captain"]["captain"], (0, 0, 100, 20))

    def test_reuses_space_in_earlier_crew_tab(self):
        self.tabs["captain"]["captain"] = (0, 0, 60, 15)
        self.meta["crew"]["crew1"] = {"pane": "crew1", "tab": "crew3", "status": "started"}
        self.meta["crew"]["crew2"] = {"pane": "crew2", "tab": "crew3", "status": "started"}
        self.tabs["crew3"] = {"crew1": (0, 0, 240, 120), "crew2": (240, 0, 240, 120)}
        self.assertEqual(self.choose()[:3], ("vertical", "crew1", "crew3"))

    def test_skips_mixed_and_closed_crew_tabs(self):
        self.tabs["captain"]["captain"] = (0, 0, 60, 15)
        self.meta["crew"]["crew1"] = {"pane": "crew1", "tab": "mixed", "status": "started"}
        self.tabs["mixed"] = {"crew1": (0, 0, 240, 120), "editor": (240, 0, 240, 120)}
        self.meta["crew"]["crew2"] = {"pane": "crew2", "tab": "closed", "status": "started"}
        self.meta["crew"]["crew3"] = {"pane": "crew3", "tab": "roomy", "status": "started"}
        self.tabs["roomy"] = {"crew3": (0, 0, 240, 120)}
        self.assertEqual(self.choose()[:3], ("vertical", "crew3", "roomy"))
        del self.tabs["mixed"]
        self.assertEqual(self.choose()[:3], ("vertical", "crew3", "roomy"))

    def test_small_tabs_are_skipped_and_closed_captain_layout_is_an_error(self):
        self.tabs["captain"]["captain"] = (0, 0, 100, 20)
        self.meta["crew"]["crew1"] = {"pane": "crew1", "tab": "crew3", "status": "started"}
        self.tabs["crew3"] = {"crew1": (0, 0, 100, 20)}
        self.assertEqual(self.choose()[:2], (None, None))
        del self.tabs["captain"]
        with self.assertRaisesRegex(CaptainError, "pane closed"):
            self.choose()

    def test_manual_directions_never_split_the_captain_down(self):
        self.assertEqual(self.recruit("horizontal")[:2], (None, None))
        self.assertEqual(self.recruit("vertical")[:2], ("vertical", "captain"))
        self.assertEqual(self.recruit("horizontal")[:2], ("horizontal", "crew2"))
        self.assertEqual(self.recruit("vertical")[:2], ("vertical", "captain"))
        self.assertEqual(self.choose("horizontal")[0], "horizontal")

    def test_explicit_pane_bypasses_the_guard_with_auto_or_manual_direction(self):
        self.meta["crew"]["crew1"] = {"pane": "crew3", "tab": "crew3", "status": "started"}
        self.tabs["crew3"] = {"crew3": (0, 0, 480, 120)}
        groups = {tab: (tab, dict.fromkeys(panes, "Crew")) for tab, panes in self.tabs.items()}
        with patch.object(Placement, "workspace_panes", return_value=groups):
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
        self.tabs["captain"]["captain"] = (0, 0, 60, 15)
        self.meta["crew"]["crew1"] = {"pane": "crew1", "tab": "crew3", "status": "started"}
        self.meta["crew"]["crew2"] = {"pane": "crew2", "tab": "crew3", "status": "started"}
        self.tabs["crew3"] = {"crew1": (0, 0, 240, 120), "crew2": (240, 0, 240, 120)}
        choose_split = Placement.choose_split
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
            self.assertEqual(choose_split(*args)[:2], ("vertical", "crew1"))
            raise CaptainError("selection checked")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(agents, "session", return_value=Session(Path(root), {"crew": {}})),
            patch.object(agents, "crew_meta") as guard,
            patch.object(Placement, "choose_split", autospec=True, side_effect=check_selection),
            self.assertRaisesRegex(CaptainError, "selection checked"),
        ):
            guard.return_value.__enter__.return_value = self.meta
            agents.create_crew(args, self.pane, Path(root))


if __name__ == "__main__":
    unittest.main()
