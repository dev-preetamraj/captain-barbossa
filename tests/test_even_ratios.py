"""A tab comes back to even ratios after churn, not only while it fills in order.

Two gaps this covers: a dismissal left its chain skewed forever, and the crew that
reopened a column a dismissal had emptied was inserted before panes the evening
arithmetic never looked at.
"""

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, config, runtime, sessions, store
from captain_barbossa.placement import Placement
from captain_barbossa.runtime import CaptainError
from captain_barbossa.sessions import Session
from tests.home_isolation import SessionCase


def pinned(**keys):
    """Pin config to the shipped defaults plus these [placement] keys."""
    merged = config.merge(config.defaults(), {"placement": keys})
    return patch.object(config, "settings", lambda: merged)


class EvenRatioTests(unittest.TestCase):
    """A roster the test owns; Herdr may only ever be asked to resize."""

    def setUp(self):
        self.pane = {"workspace_id": "w1", "tab_id": "captain", "pane_id": "captain"}
        self.meta = {"crew": {}}
        self.resizes = []
        self.board = None
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(runtime, "herdr", side_effect=self._herdr))

    def _herdr(self, *args, **_):
        if args[:2] != ("pane", "resize"):
            raise AssertionError(f"placement asked Herdr: {args}")
        self.resizes.append((args[3], args[5], args[7]))
        return {"resize": {"changed": True}}

    def place(self, name, column, row, tab="captain"):
        self.meta["crew"][name] = {
            "pane": name,
            "tab": tab,
            "status": "started",
            "column": column,
            "row": row,
        }

    def placement(self):
        return Placement(self.pane, Session(None, self.meta), self.board)

    def recruit(self):
        args = SimpleNamespace(direction=None, split_pane="auto")
        return self.placement().choose_split(args, "pane")

    def plan(self, name):
        return self.placement().even_after_close(self.meta["crew"][name])

    def test_reopening_an_emptied_column_widens_the_pane_it_splits(self):
        """Column 2 was emptied by a dismissal, so the new crew lands in the middle of
        the row: the captain has to grow to two thirds before halving into even thirds.
        """
        self.place("b", 3, 1)
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.recruit()
        self.assertEqual((spot.column, spot.row, spot.pane), (2, 1, "captain"))
        self.assertEqual(self.resizes, [("captain", "right", "0.1667")])
        self.assertAlmostEqual(spot.ratio, 0.5)

    def test_reopening_a_column_with_panes_on_both_sides_shrinks_left_and_widens(self):
        """Column 3 of four was emptied, so the new pane lands between the captain and
        column 4: the captain shrinks to a quarter and column 2 widens to hold the pair.
        """
        self.place("a", 2, 1)
        self.place("b", 4, 1)
        with pinned(captain_tab=[1, 2, 2, 2]):
            spot = self.recruit()
        self.assertEqual((spot.column, spot.row, spot.pane), (3, 1, "a"))
        self.assertEqual(
            self.resizes,
            [
                ("captain", "left", f"{1 / 3 - 1 / 4:.4f}"),
                ("a", "right", f"{2 / 3 - 1 / 2:.4f}"),
            ],
        )

    def test_appending_a_column_still_only_shrinks_the_earlier_panes(self):
        """The fill-in-order case keeps its old calls: nothing beyond the split to widen."""
        self.place("a", 2, 1)
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.recruit()
        self.assertEqual((spot.column, spot.pane), (3, "a"))
        self.assertEqual(self.resizes, [("captain", "left", "0.1667")])

    def test_a_dismissal_grows_the_panes_before_it_back_to_an_even_share(self):
        self.place("a", 2, 1)
        self.place("b", 3, 1)
        plan = self.plan("a")
        self.assertEqual([(pane, towards) for pane, towards, _ in plan], [("captain", "right")])
        self.assertAlmostEqual(plan[0][2], 1 / 2 - 1 / 3)

    def test_a_dismissal_leaves_the_panes_after_it_alone(self):
        """Herdr hands the closed pane's space to the panes after it, which land exactly
        on their new share; a plan that moved them too would overshoot.
        """
        self.place("a", 1, 1, tab="crew")
        self.place("b", 2, 1, tab="crew")
        self.place("c", 3, 1, tab="crew")
        self.assertEqual(self.plan("a"), [])
        self.assertEqual([pane for pane, _, _ in self.plan("c")], ["a"])

    def test_a_dismissal_inside_a_column_evens_that_column(self):
        self.place("a", 1, 1, tab="crew")
        self.place("b", 1, 2, tab="crew")
        self.place("c", 1, 3, tab="crew")
        plan = self.plan("c")
        self.assertEqual([(pane, towards) for pane, towards, _ in plan], [("a", "down")])
        self.assertAlmostEqual(plan[0][2], 1 / 2 - 1 / 3)

    def test_the_last_crew_in_a_chain_leaves_nothing_to_even(self):
        self.place("a", 2, 1)
        self.assertEqual(self.plan("a"), [])

    def test_a_dashboard_is_not_a_pane_the_row_chain_evens(self):
        """The dashboard is nested inside the captain's own slot, so the captain's right
        edge is still the column boundary and the dashboard is in no chain.
        """
        self.board = "dash"
        self.place("a", 2, 1)
        with pinned(captain_tab=[1, 2, 2]):
            spot = self.recruit()
        self.assertEqual((spot.column, spot.pane), (3, "a"))
        self.assertEqual(self.resizes, [("captain", "left", "0.1667")])

    def test_a_crew_placed_by_hand_moves_nothing(self):
        self.meta["crew"]["a"] = {"pane": "a", "tab": "captain", "status": "started"}
        self.assertEqual(self.placement().even_after_close(self.meta["crew"]["a"]), [])


class DismissEvensTheTabTests(SessionCase):
    """dismiss_crew evens the tab after Herdr has closed the pane, never before."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.current.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": "jack",
                "pane": "w1:p2",
                "tab": self.pane["tab_id"],
                "column": 2,
                "row": 1,
                "status": "started",
            },
            "will": {
                "id": "will",
                "name": "Will",
                "agent": "will",
                "pane": "w1:p3",
                "tab": self.pane["tab_id"],
                "column": 3,
                "row": 1,
                "status": "started",
            },
        }
        store.write_json(self.current.meta_path, self.current.meta)
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))

    def test_the_closed_pane_is_evened_out_after_it_closes(self):
        calls = []

        def herdr(*args, **_):
            calls.append(args)
            return {"resize": {"changed": True}}

        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", side_effect=herdr),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        self.assertEqual(
            calls,
            [
                ("pane", "close", "w1:p2"),
                (
                    "pane",
                    "resize",
                    "--pane",
                    "w1:p1",
                    "--direction",
                    "right",
                    "--amount",
                    "0.1667",
                ),
            ],
        )

    def test_a_refused_resize_still_dismisses_the_crew(self):
        """Geometry is cosmetic: a tab left uneven must not cost the dismissal."""

        def herdr(*args, **_):
            if args[:2] == ("pane", "resize"):
                return {"resize": {"changed": False}}
            return {}

        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", side_effect=herdr),
            contextlib.redirect_stderr(io.StringIO()) as problem,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        self.assertIn("Dismissed Jack.", output.getvalue())
        self.assertIn("not re-evened", problem.getvalue())
        self.assertEqual(
            store.read_json(self.current.meta_path)["crew"]["jack"]["status"], "dismissed"
        )

    def test_a_pane_already_gone_is_not_evened_out(self):
        """Herdr rebalances the tab itself when a pane disappears, so applying the plan
        read before the close would move the survivors twice.
        """
        calls = []

        def herdr(*args, **_):
            calls.append(args)
            if args[:2] == ("pane", "close"):
                raise CaptainError("herdr pane close failed: pane_not_found")
            return {"resize": {"changed": True}}

        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(agents.runtime, "herdr", side_effect=herdr),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        self.assertIn("Dismissed Jack.", output.getvalue())
        self.assertEqual(calls, [("pane", "close", "w1:p2")])

    def test_a_chain_too_long_to_even_still_dismisses_the_crew(self):
        """Reading the plan is as cosmetic as applying it: re_even refuses a chain of 12
        panes, and that refusal must not reach the dismissal.
        """
        for column in range(4, 13):
            self.current.meta["crew"][f"c{column}"] = {
                "id": f"c{column}",
                "name": f"C{column}",
                "agent": f"c{column}",
                "pane": f"w1:p{column}",
                "tab": self.pane["tab_id"],
                "column": column,
                "row": 1,
                "status": "started",
            }
        store.write_json(self.current.meta_path, self.current.meta)
        calls = []

        args = SimpleNamespace(session=self.current.meta["id"], name="Jack")
        with (
            patch.object(
                agents.runtime, "herdr", side_effect=lambda *a, **_: calls.append(a) or {}
            ),
            contextlib.redirect_stderr(io.StringIO()) as problem,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        self.assertIn("Dismissed Jack.", output.getvalue())
        self.assertIn("not re-evened", problem.getvalue())
        self.assertEqual(calls, [("pane", "close", "w1:p2")])


if __name__ == "__main__":
    unittest.main()
