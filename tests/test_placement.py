"""Crew placement into declared tab shapes, decided from the roster, evened by Herdr."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, cli, config, layout, runtime
from captain_barbossa.memory import Session
from captain_barbossa.placement import Placement
from captain_barbossa.runtime import CaptainError


def pinned(**keys):
    """Pin config to the shipped defaults plus these [placement] keys."""
    merged = config.merge(config.defaults(), {"placement": keys})
    return patch.object(config, "settings", lambda: merged)


class GridHarness(unittest.TestCase):
    """A roster the test owns. Auto placement may only ever call Herdr to resize an

    existing pane, evening out a column or row a new crew is about to join; any other
    call is a bug and fails the test.
    """

    def setUp(self):
        self.pane = {"workspace_id": "w1", "tab_id": "captain", "pane_id": "captain"}
        self.meta = {"crew": {}}
        self.tabs = 0
        self.herdr_calls = []
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.api = self.enterContext(patch.object(runtime, "herdr", side_effect=self._herdr))

    def _herdr(self, *args, **_):
        if args[:2] != ("pane", "resize"):
            raise AssertionError(f"placement asked Herdr: {args}")
        self.herdr_calls.append(args)
        return {"resize": {"changed": True}}

    def choose(self, direction=None, target="auto", placement="pane"):
        args = SimpleNamespace(direction=direction, split_pane=target)
        return Placement(self.pane, Session(None, self.meta)).choose_split(args, placement)

    def recruit(self, direction=None, name=None):
        """Place one crew and record it the way create_crew does."""
        spot = self.choose(direction)
        name = name or f"crew{len(self.meta['crew']) + 1}"
        if spot.pane is None:
            self.tabs += 1
            tab = f"newtab{self.tabs}"
        else:
            tab = spot.tab
        self.meta["crew"][name] = {
            "pane": name,
            "tab": tab,
            "status": "started",
            "column": spot.column,
            "row": spot.row,
        }
        return spot, name, tab

    def place(self, name, tab, column, row, pane=None):
        """Write a crew straight into a slot, to set a roster up without walking to it."""
        self.meta["crew"][name] = {
            "pane": pane or name,
            "tab": tab,
            "status": "started",
            "column": column,
            "row": row,
        }

    def walk(self, count, **keys):
        """Recruit count crew under these shapes; returns (slot, split pane, direction)."""
        placed = []
        with pinned(**keys):
            for _ in range(count):
                spot, _, tab = self.recruit()
                placed.append(((spot.column, spot.row), spot.pane, spot.direction, tab))
        return placed


class FillOrderTests(GridHarness):
    """Breadth first: a new column opens before anything stacks."""

    def test_the_captain_tab_fills_its_declared_shape(self):
        placed = self.walk(5, captain_tab=[1, 2, 2])
        self.assertEqual(
            placed[:4],
            [
                ((2, 1), "captain", "vertical", "captain"),
                ((3, 1), "crew1", "vertical", "captain"),
                ((2, 2), "crew1", "horizontal", "captain"),
                ((3, 2), "crew2", "horizontal", "captain"),
            ],
        )
        # The fifth has nowhere left in [1, 2, 2], so it opens a tab.
        self.assertEqual(placed[4][:3], ((1, 1), None, None))

    def test_a_crew_tab_fills_its_own_shape(self):
        placed = self.walk(8, captain_tab=[1], crew_tab=[2, 2, 3])
        # captain_tab = [1] holds no crew, so the first opens a tab and takes its root.
        self.assertEqual(placed[0][:3], ((1, 1), None, None))
        tab = placed[0][3]
        self.assertEqual(
            [(slot, pane, direction) for slot, pane, direction, _ in placed[1:7]],
            [
                ((2, 1), "crew1", "vertical"),
                ((3, 1), "crew2", "vertical"),
                ((1, 2), "crew1", "horizontal"),
                ((2, 2), "crew2", "horizontal"),
                ((3, 2), "crew3", "horizontal"),
                ((3, 3), "crew6", "horizontal"),
            ],
        )
        self.assertEqual({row[3] for row in placed[1:7]}, {tab})
        # Seven crew fill [2, 2, 3]; the eighth starts another tab.
        self.assertEqual(placed[7][1], None)

    def test_a_depth_above_one_stacks_crew_under_the_captain(self):
        placed = self.walk(3, captain_tab=[2, 2])
        self.assertEqual(
            [(slot, pane, direction) for slot, pane, direction, _ in placed],
            [
                # Row 1 opens column 2 first; row 2 then walks left to right, so the
                # slot under the captain comes before the second row of column 2.
                ((2, 1), "captain", "vertical"),
                ((1, 2), "captain", "horizontal"),
                ((2, 2), "crew1", "horizontal"),
            ],
        )

    def test_a_new_splits_own_ratio_is_always_half_whatever_the_declared_depth(self):
        """A pane's own split is always even against the one new pane joining it,

        whatever the declared shape's eventual depth; re_even (tested below) is what
        brings the rest of the chain back to an even share once that pane exists.
        """
        with pinned(captain_tab=[1, 2, 2]):
            self.assertAlmostEqual(self.recruit()[0].ratio, 0.5)  # column 2 of 3
            self.assertAlmostEqual(self.recruit()[0].ratio, 0.5)  # column 3 of 3
            self.assertAlmostEqual(self.recruit()[0].ratio, 0.5)  # row 2 of 2
        with pinned(captain_tab=[1], crew_tab=[3]):
            self.recruit()
            self.assertAlmostEqual(self.recruit()[0].ratio, 0.5)  # row 2 of 3
            self.assertAlmostEqual(self.recruit()[0].ratio, 0.5)  # row 3 of 3

    def test_a_third_pane_arriving_resizes_the_first_back_to_a_third(self):
        with pinned(captain_tab=[1, 2, 2]):
            self.recruit()  # opens column 2: captain and column 2 are already even
            self.herdr_calls.clear()
            self.recruit()  # opens column 3: the captain alone must give ground
        self.assertEqual(
            self.herdr_calls,
            [("pane", "resize", "--pane", "captain", "--direction", "left", "--amount", "0.1667")],
        )

    def test_a_fourth_pane_arriving_resizes_the_first_two_back_to_a_quarter(self):
        with pinned(captain_tab=[1], crew_tab=[4]):
            self.recruit(name="crew1")
            self.recruit(name="crew2")
            self.recruit(name="crew3")
            self.herdr_calls.clear()
            self.recruit(name="crew4")
        self.assertEqual(
            self.herdr_calls,
            [
                ("pane", "resize", "--pane", "crew1", "--direction", "up", "--amount", "0.0833"),
                ("pane", "resize", "--pane", "crew2", "--direction", "up", "--amount", "0.1667"),
            ],
        )

    def test_a_shape_too_deep_for_herdrs_resizable_range_is_reported_not_approximated(self):
        with pinned(captain_tab=[1], crew_tab=[11]):
            for _ in range(10):
                self.recruit()
            with self.assertRaisesRegex(CaptainError, "cannot size 11 panes evenly"):
                self.recruit()

    def test_herdr_declining_a_resize_is_reported_not_approximated(self):
        with pinned(captain_tab=[1, 2, 2]):
            self.recruit()
            with (
                patch.object(runtime, "herdr", return_value={"resize": {"changed": False}}),
                self.assertRaisesRegex(CaptainError, "would not resize"),
            ):
                self.recruit()


class DeterminismTests(GridHarness):
    """Same roster and shape in, same slot and split out."""

    def roster(self):
        self.place("a", "captain", 2, 1)
        self.place("b", "captain", 3, 1)
        self.place("c", "captain", 2, 2)

    def test_the_same_roster_always_gives_the_same_slot(self):
        self.roster()
        with pinned(captain_tab=[1, 2, 2]):
            answers = {
                (spot.column, spot.row, spot.pane, spot.direction)
                for spot in (self.choose() for _ in range(20))
            }
        self.assertEqual(answers, {(3, 2, "b", "horizontal")})

    def test_the_roster_order_in_the_file_does_not_change_the_answer(self):
        """A re-serialised session file lists crew in some order; the slot cannot move."""
        self.roster()
        records = dict(self.meta["crew"])
        answers = set()
        with pinned(captain_tab=[1, 2, 2]):
            for order in ("abc", "cba", "bac", "cab"):
                self.meta["crew"] = {name: records[name] for name in order}
                spot = self.choose()
                answers.add((spot.column, spot.row, spot.pane, spot.direction))
        self.assertEqual(answers, {(3, 2, "b", "horizontal")})

    def test_the_answer_moves_with_the_roster_so_the_tests_above_are_not_vacuous(self):
        with pinned(captain_tab=[1, 2, 2]):
            first = self.choose()
            self.roster()
            later = self.choose()
        self.assertNotEqual(
            (first.column, first.row, first.pane), (later.column, later.row, later.pane)
        )

    def test_placement_only_ever_asks_herdr_to_resize(self):
        """Deciding a slot may re-even existing panes, but never lists, splits, or reads:

        the harness fails the test on any other call, so surviving this walk is the
        assertion.
        """
        self.roster()
        with pinned(captain_tab=[1, 2, 2]):
            self.choose()
            self.walk(3, captain_tab=[1, 2, 2])
        self.assertTrue(all(call[:2] == ("pane", "resize") for call in self.herdr_calls))


class DismissalTests(GridHarness):
    """A dismissal frees one place in its column; the survivors keep their panes."""

    def setUp(self):
        super().setUp()
        for name, column, row in (("a", 2, 1), ("b", 3, 1), ("c", 2, 2), ("d", 3, 2)):
            self.place(name, "captain", column, row)

    def dismiss(self, *names):
        for name in names:
            self.meta["crew"][name]["status"] = "dismissed"

    def test_dismissing_the_middle_of_a_column_rebuilds_the_same_slot(self):
        self.dismiss("c")
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.choose()
        self.assertEqual((spot.column, spot.row), (2, 2))
        self.assertEqual((spot.pane, spot.direction), ("a", "horizontal"))

    def test_dismissing_the_top_of_a_column_splits_the_survivor(self):
        self.dismiss("a")
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.choose()
        # Column 2 holds only c now, whose pane grew into the space it left.
        self.assertEqual((spot.column, spot.row), (2, 2))
        self.assertEqual((spot.pane, spot.direction), ("c", "horizontal"))

    def test_an_emptied_column_is_opened_again_from_its_left_neighbour(self):
        self.dismiss("a", "c")
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.choose()
        self.assertEqual((spot.column, spot.row), (2, 1))
        self.assertEqual((spot.pane, spot.direction), ("captain", "vertical"))

    def test_an_emptied_leftmost_crew_column_is_skipped_not_rebuilt(self):
        """Herdr splits right and down only, so nothing can open to the left of column 2."""
        self.meta["crew"] = {}
        self.place("a", "t2", 1, 1)
        self.place("b", "t2", 2, 1)
        self.meta["crew"]["a"]["status"] = "dismissed"
        with pinned(captain_tab=[1], crew_tab=[2, 2]):
            spot = self.choose()
        # Column 1 is free on paper but unreachable, so the crew stacks in column 2.
        self.assertEqual((spot.column, spot.row), (2, 2))
        self.assertEqual((spot.pane, spot.direction), ("b", "horizontal"))


class OverflowTests(GridHarness):
    """The captain tab, then crew tabs in recruitment order, then a new tab."""

    def test_the_chain_runs_in_order(self):
        with pinned(captain_tab=[1, 1], crew_tab=[1, 1]):
            first = self.recruit()[0]
            self.assertEqual(first.tab, "captain")

            # The captain tab is full, so the next opens a crew tab.
            second, _, older = self.recruit()
            self.assertIsNone(second.pane)

            # That tab has one place left, and it is used before any new tab.
            third = self.recruit()[0]
            self.assertEqual((third.tab, third.column), (older, 2))

            # Both tabs full: a third tab opens.
            fourth, _, newer = self.recruit()
            self.assertIsNone(fourth.pane)
            self.assertNotEqual(newer, older)

    def test_the_oldest_crew_tab_with_room_wins(self):
        self.place("a", "captain", 2, 1)
        self.place("older", "t2", 1, 1)
        self.place("newer", "t3", 1, 1)
        with pinned(captain_tab=[1, 1], crew_tab=[2]):
            spot = self.choose()
        self.assertEqual((spot.tab, spot.pane, spot.row), ("t2", "older", 2))


class HandPlacementTests(GridHarness):
    """Explicit flags win, and what they place holds no slot."""

    def test_an_explicit_tab_takes_no_slot_and_asks_nothing(self):
        spot = self.choose(target=None, placement="tab")
        self.assertEqual((spot.pane, spot.tab, spot.column), (None, None, None))
        self.api.assert_not_called()

    def test_an_explicit_pane_and_direction_bypass_the_shape(self):
        with pinned(captain_tab=[1, 2]):
            spot = self.choose("horizontal", "captain")
        self.assertEqual((spot.pane, spot.direction), ("captain", "horizontal"))
        self.assertIsNone(spot.column)
        self.assertIsNone(spot.ratio)

    def test_an_explicit_pane_with_auto_direction_splits_beside_it(self):
        with pinned(captain_tab=[1, 2]):
            spot = self.choose("auto", "captain")
        self.assertEqual((spot.pane, spot.direction), ("captain", "vertical"))
        self.assertIsNone(spot.column)

    def test_a_hand_placed_crew_does_not_move_the_next_slot(self):
        # create_crew records a hand placement as a present but empty slot.
        self.meta["crew"]["byhand"] = {
            "pane": "byhand",
            "tab": "captain",
            "status": "started",
            "column": None,
            "row": None,
        }
        with pinned(captain_tab=[1, 2]):
            spot = self.choose()
        self.assertEqual((spot.column, spot.row, spot.pane), (2, 1, "captain"))


class UpgradedSessionTests(GridHarness):
    """Crew recruited before slots were recorded: their tab's geometry is unknown."""

    def legacy(self, name, tab):
        """A roster entry as versions before the grid wrote it: no column key at all."""
        self.meta["crew"][name] = {"pane": name, "tab": tab, "status": "started"}

    def test_a_tab_holding_pre_upgrade_crew_is_never_split_over(self):
        self.legacy("will", "captain")
        with pinned(captain_tab=[1, 2]):
            spot = self.choose()
        # Splitting the captain's pane again would land the new crew on top of Will.
        self.assertIsNone(spot.pane)
        self.assertIn("unmapped", spot.reason)
        # A new tab's id comes back from Herdr, so the spot must not carry a stale one.
        self.assertIsNone(spot.tab)

    def test_a_tab_with_no_pre_upgrade_crew_still_fills_its_shape(self):
        self.legacy("will", "othertab")
        with pinned(captain_tab=[1, 2]):
            spot = self.choose()
        self.assertEqual((spot.column, spot.row, spot.pane), (2, 1, "captain"))

    def test_a_dismissed_pre_upgrade_crew_frees_its_tab_again(self):
        self.legacy("will", "captain")
        self.meta["crew"]["will"]["status"] = "dismissed"
        with pinned(captain_tab=[1, 2]):
            spot = self.choose()
        self.assertEqual((spot.column, spot.row, spot.pane), (2, 1, "captain"))

    def test_a_forced_direction_drops_the_ratio(self):
        with pinned(captain_tab=[1, 2]):
            self.assertIsNone(self.choose("horizontal").ratio)
            self.assertIsNotNone(self.choose().ratio)


class ShapeValidationTests(unittest.TestCase):
    """A malformed shape fails the command, naming the file and the value."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".captain").mkdir()
        self.settings = self.root / ".captain" / "settings.toml"
        self.enterContext(patch.dict(__import__("os").environ, {"HOME": str(self.root / "nohome")}))
        self.enterContext(patch.object(config, "project_root", return_value=self.root))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def write(self, body):
        self.settings.write_text(f"[placement]\n{body}\n", encoding="utf-8")
        config.settings.cache_clear()

    def test_the_shipped_shapes_are_valid(self):
        self.assertEqual(layout.shape("captain_tab"), (1, 2))
        self.assertEqual(layout.shape("crew_tab"), (2, 2))

    def test_a_malformed_shape_names_the_file_the_value_and_the_fault(self):
        for body, fault in (
            ("captain_tab = [1, 0, 2]", "column 2 is 0"),
            ("captain_tab = [1, -3]", "column 2 is -3"),
            ('captain_tab = [1, "2"]', "column 2 is '2'"),
            ("captain_tab = [1, 2.5]", "column 2 is 2.5"),
            ("captain_tab = [1, true]", "column 2 is True"),
            ("captain_tab = []", "the list is empty"),
            ('captain_tab = "wide"', "it is not a list"),
        ):
            with self.subTest(body=body):
                self.write(body)
                with self.assertRaises(CaptainError) as raised:
                    layout.shape("captain_tab")
                message = str(raised.exception)
                self.assertIn("[placement] captain_tab must be a list", message)
                self.assertIn(str(self.settings), message)
                self.assertIn(fault, message)

    def test_a_malformed_shape_is_never_quietly_replaced_by_the_default(self):
        self.write("crew_tab = [0]")
        with self.assertRaises(CaptainError):
            layout.shape("crew_tab")


class RecruitLockTests(GridHarness):
    def test_recruit_selects_from_fresh_metadata_under_the_crew_lock(self):
        self.place("a", "captain", 2, 1)
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

        def check_selection(*call):
            guard.return_value.__enter__.assert_called_once()
            # The fresh roster already holds column 2, so the next slot is its second row.
            self.assertEqual(choose_split(*call).row, 2)
            raise CaptainError("selection checked")

        with (
            tempfile.TemporaryDirectory() as root,
            pinned(captain_tab=[1, 2]),
            patch.object(agents, "session", return_value=Session(Path(root), {"crew": {}})),
            patch.object(agents, "crew_meta") as guard,
            patch.object(Placement, "choose_split", autospec=True, side_effect=check_selection),
            self.assertRaisesRegex(CaptainError, "selection checked"),
        ):
            guard.return_value.__enter__.return_value = self.meta
            agents.create_crew(args, self.pane, Path(root))


if __name__ == "__main__":
    unittest.main()
