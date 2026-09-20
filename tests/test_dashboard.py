import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import dashboard, memory, runtime
from captain_barbossa.usage import RATE_WINDOW

# A tall, wide pane: individual tests narrow or shorten it on purpose.
ROOMY = (81, 24)


def usage(tokens=None, cost=None, rate=None, context=None, limit=None, model=None):
    """A usage_for_events result. The four token kinds survive; the frame ignores them."""
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tokens": tokens,
        "cost": cost,
        "rate": rate,
        "context": context,
        "limit": limit,
        "model": model,
    }


class DashboardCase(unittest.TestCase):
    """A session with a captain pane, two current crew and one dismissed."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            )
        )
        pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, pane, create=True)
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": "c-abc-jack",
                "provider": "claude",
                "model": "claude-sonnet-5",
                "status": "working",
            },
            "will": {
                "id": "will",
                "name": "Will",
                "agent": "c-abc-will",
                "provider": "codex",
                "status": "idle",
            },
            "gibbs": {
                "id": "gibbs",
                "name": "Gibbs",
                "agent": "c-abc-gibbs",
                "provider": "claude",
                "status": "dismissed",
            },
        }
        memory.write_json(self.directory / "session.json", self.meta)
        self.current = memory.Session(self.directory, self.meta)

    def render(self, usages=None, agents=(), size=ROOMY, herdr=None):
        """One frame with usage and Herdr stubbed; `usages` maps a crew id to its usage."""
        usages = usages or {}
        herdr = herdr or (lambda *a, **k: {"agents": list(agents)})
        with (
            patch.object(
                dashboard,
                "usage_for_events",
                side_effect=lambda events: usages.get(Path(str(events)).stem),
            ),
            patch.object(dashboard.runtime, "herdr", side_effect=herdr),
        ):
            return dashboard.render(self.current, size=size)

    def line(self, output, prefix):
        return next(line for line in output.splitlines() if line.startswith(prefix))

    def assertHeader(self, output, crew_count):
        self.assertRegex(
            output.splitlines()[0],
            rf"^session {re.escape(self.meta['id'])}  crew {crew_count}  \d{{2}}:\d{{2}}:\d{{2}}$",
        )


class DashboardRenderTests(DashboardCase):
    def test_renders_usage_row_dismissed_skip_and_total(self):
        output = self.render(
            {"jack": usage(tokens=130_000, cost=0.07, rate=0.05, context=44_000, limit=400_000)}
        )
        self.assertHeader(output, 2)
        self.assertIn("NAME", self.line(output, "NAME"))
        self.assertNotIn("Gibbs", output)

        jack = self.line(output, "Jack")
        self.assertIn("130k", jack)
        self.assertIn("$0.07", jack)
        self.assertIn("#....  11%", jack)
        self.assertIn("working", jack)

        will = self.line(output, "Will")
        self.assertIn("-", will.split())

        self.assertIn("130k+?", self.line(output, "TOTAL "))

    def test_columns_are_name_agent_status_ctx_tokens_cost_rate(self):
        output = self.render()
        self.assertEqual(
            re.split(r" {2,}", self.line(output, "NAME")),
            ["NAME", "AGENT", "STATUS", "CTX NOW", "CUM TOK", "CUM $ $/h 10m"],
        )

    def test_the_rate_header_names_its_trailing_window(self):
        """An idle crew's honest $0.00 reads as a broken column unless the header scopes it."""
        usages = {"jack": usage(tokens=130_000, cost=1.22, rate=0.0)}
        output = self.render(usages)
        self.assertIn(f"$/h {RATE_WINDOW // 60}m", self.line(output, "NAME"))
        self.assertIn("$0.00", self.line(output, "Jack"))

    def test_the_headers_separate_the_cumulative_columns_from_the_live_one(self):
        """A cumulative count beside a percentage that resets reads as one unit."""
        usages = {"jack": usage(tokens=1_320_829, cost=1.22, context=68_540, limit=1_000_000)}
        header = self.line(self.render(usages), "NAME")
        self.assertIn("CUM TOK", header)
        self.assertIn("CUM $", header)
        self.assertIn("CTX NOW", header)
        # 1.32M cumulative against 7% of a 1M window is the frame that prompted this.
        self.assertRegex(self.line(self.render(usages), "Jack"), r"\.\.\.\.\.\s+7%\s+1.32M")

    def test_context_renders_a_bar_and_a_percentage(self):
        output = self.render({"jack": usage(tokens=1, cost=0.0, context=83_000, limit=225_000)})
        self.assertIn("##...  37%", self.line(output, "Jack"))

    def test_context_is_a_dash_when_the_limit_is_unknown(self):
        output = self.render({"jack": usage(tokens=1_000, cost=0.0, context=225_000, limit=None)})
        jack = self.line(output, "Jack")
        self.assertNotIn("%", jack)
        self.assertIn("-", jack.split())

    def test_live_status_overrides_stored_status(self):
        output = self.render(agents=[{"name": "c-abc-jack", "agent_status": "blocked"}])
        jack = self.line(output, "Jack")
        self.assertIn("blocked", jack)
        self.assertNotIn("working", jack)

    def test_live_status_lookup_falls_back_to_stored_on_herdr_error(self):
        def boom(*args, **kwargs):
            raise runtime.CaptainError("no herdr")

        output = self.render(herdr=boom)
        self.assertIn("working", self.line(output, "Jack"))

    def test_bad_crew_degrades_to_dashes_instead_of_crashing(self):
        def explode(events):
            raise RuntimeError("boom")

        with (
            patch.object(dashboard, "usage_for_events", side_effect=explode),
            patch.object(dashboard.runtime, "herdr", return_value={"agents": []}),
        ):
            output = dashboard.render(self.current, size=ROOMY)
        self.assertIn("Jack", output)
        self.assertIn("-", self.line(output, "Jack").split())
        self.assertTrue(output.splitlines()[-1].endswith("+? incomplete"))

    def test_no_crew_prints_header_and_placeholder(self):
        self.meta["crew"] = {}
        memory.write_json(self.directory / "session.json", self.meta)
        self.current = memory.Session(self.directory, self.meta)
        output = self.render()
        self.assertHeader(output, 0)
        self.assertTrue(output.endswith("No crew."))

    def test_crew_recruited_after_the_first_frame_appears_in_the_next(self):
        first = self.render()
        self.assertNotIn("Elizabeth", first)
        self.assertHeader(first, 2)

        meta = memory.read_json(self.directory / "session.json")
        meta["crew"]["elizabeth"] = {
            "id": "elizabeth",
            "name": "Elizabeth",
            "agent": "c-abc-elizabeth",
            "provider": "claude",
            "status": "working",
        }
        memory.write_json(self.directory / "session.json", meta)

        second = self.render()
        self.assertIn("Elizabeth", second)
        self.assertHeader(second, 3)

    def test_captain_row_leads_the_table_with_status_matched_by_pane(self):
        memory.write_json(self.directory / "captain.json", {"provider": "claude", "pane": "w1:p1"})
        output = self.render(agents=[{"pane_id": "w1:p1", "agent_status": "blocked"}])
        lines = output.splitlines()
        captain = lines[lines.index(self.line(output, "NAME")) + 1]
        self.assertTrue(captain.startswith("CAPTAIN"))
        self.assertIn("blocked", captain)
        # No captain transcript yet: usage cells stay unknown.
        self.assertIn("-", captain.split())
        self.assertIn("$?", captain.split())
        self.assertHeader(output, 2)

    def test_captain_row_falls_back_to_transcript_model(self):
        memory.write_json(self.directory / "captain.json", {"provider": "claude", "pane": "w1:p1"})
        output = self.render({"captain": usage(tokens=1_500, cost=0.01, model="claude-sonnet-5")})
        self.assertIn("claude/sonnet-5", self.line(output, "CAPTAIN"))

    def test_session_file_model_wins_over_the_recruit_time_record(self):
        # A model switched in the Codex TUI or via /model has to show through.
        output = self.render({"jack": usage(tokens=1_500, cost=0.01, model="claude-opus-5")})
        jack = self.line(output, "Jack")
        self.assertIn("claude/opus-5", jack)
        self.assertNotIn("sonnet", jack)

    def test_model_falls_back_to_events_when_no_transcript_yet(self):
        with patch.object(dashboard, "model_for_events", return_value="claude-opus-5"):
            output = self.render()
        will = self.line(output, "Will")
        self.assertIn("codex/claude-opus-5", will)
        self.assertIn("-", will.split())

    def test_transcript_model_wins_over_events_model(self):
        with patch.object(dashboard, "model_for_events", return_value="claude-haiku-4-5"):
            output = self.render({"will": usage(tokens=1_500, cost=0.01, model="gpt-5.6-terra")})
        will = self.line(output, "Will")
        self.assertIn("codex/gpt-5.6-terra", will)
        self.assertNotIn("haiku", will)

    def test_agent_strips_a_dated_model_suffix(self):
        output = self.render({"jack": usage(tokens=1, cost=0.0, model="claude-haiku-4-5-20251001")})
        self.assertIn("claude/haiku-4-5", self.line(output, "Jack"))

    def test_captain_row_is_absent_without_captain_json(self):
        self.assertNotIn("CAPTAIN ", self.render())


class AccountingTests(DashboardCase):
    """The binding rules from docs/dashboard-layout.md: retired spend, totals, unknowns."""

    def frame(self, size=ROOMY):
        memory.write_json(self.directory / "captain.json", {"provider": "claude", "pane": "w1:p1"})
        return self.render(
            {
                "captain": usage(2_700_000, 2.66, 1.84, 70_000, 1_000_000, "claude-opus-5"),
                "jack": usage(130_000, 0.07, 0.05, 44_000, 400_000, "gpt-5.6-terra"),
                "will": usage(366_000, 0.09, 0.06, 62_000, 200_000, "claude-haiku-4-5"),
                "gibbs": usage(3_104_000, 0.56, None, None, None, "claude-haiku-4-5"),
            },
            size=size,
        )

    def test_retired_crew_have_no_row_but_stay_in_the_total(self):
        output = self.frame()
        self.assertNotIn("Gibbs", output)
        total = self.line(output, "TOTAL ")
        self.assertIn("6.30M", total)
        self.assertIn("$3.38", total)
        # Retired crew burn nothing, so the rate is the current roster's alone.
        self.assertIn("$1.95", total)
        self.assertEqual(
            output.splitlines()[-1],
            "TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded",
        )

    def test_total_never_shrinks_when_a_crew_is_dismissed(self):
        before = self.line(self.frame(), "TOTAL ")
        meta = memory.read_json(self.directory / "session.json")
        meta["crew"]["will"]["status"] = "dismissed"
        memory.write_json(self.directory / "session.json", meta)
        output = self.frame()
        after = self.line(output, "TOTAL ")
        self.assertNotIn("Will", output)
        self.assertIn("6.30M", after)
        self.assertIn("$3.38", after)
        self.assertEqual(before.split()[-4:-1], after.split()[-4:-1])
        # The rate is current-roster only, so it falls by the dismissed crew's share.
        self.assertIn("$1.89", after)
        self.assertIn("retired(2): 3.47M tok/$0.65", output.splitlines()[-1])

    def test_footer_survives_with_no_retired_crew(self):
        meta = memory.read_json(self.directory / "session.json")
        del meta["crew"]["gibbs"]
        memory.write_json(self.directory / "session.json", meta)
        output = self.frame()
        self.assertEqual(
            output.splitlines()[-1], "TOTAL is session-cumulative; USD list est; rounded"
        )

    def test_unknown_parts_mark_the_total_and_the_footer(self):
        output = self.render({"jack": usage(130_000, 0.07, 0.05, 44_000, 400_000)})
        total = self.line(output, "TOTAL ")
        self.assertIn("130k+?", total)
        self.assertIn("$0.07+?", total)
        self.assertTrue(output.splitlines()[-1].endswith("+? incomplete"))

    def test_missing_pricing_renders_a_question_mark_not_a_zero(self):
        output = self.render({"jack": usage(tokens=130_000, cost=None, rate=None)})
        jack = self.line(output, "Jack")
        self.assertIn("130k", jack)
        self.assertIn("$?", jack.split())
        self.assertNotIn("$0.00", jack)

    def test_a_positive_amount_below_half_a_cent_is_not_shown_as_zero(self):
        output = self.render({"jack": usage(tokens=10, cost=0.002, rate=0.0)})
        jack = self.line(output, "Jack")
        self.assertIn("<$0.01", jack)
        self.assertIn("$0.00", jack)


class FitTests(DashboardCase):
    """Height and width degradation."""

    def crowd(self, count):
        memory.write_json(self.directory / "captain.json", {"provider": "claude", "pane": "w1:p1"})
        meta = memory.read_json(self.directory / "session.json")
        meta["crew"] = {
            f"c{index}": {
                "id": f"c{index}",
                "name": f"Crew{index}",
                "agent": f"a{index}",
                "provider": "codex",
                "model": "gpt-5.6-terra",
                "status": "idle",
            }
            for index in range(count)
        }
        memory.write_json(self.directory / "session.json", meta)
        each = usage(210_000, 0.13, 0.12, 44_000, 400_000, "gpt-5.6-terra")
        return {"captain": usage(100_000, 1.0, 0.5, 70_000, 1_000_000, "claude-opus-5")} | {
            f"c{index}": each for index in range(count)
        }

    def test_eight_crew_and_the_captain_fit_in_twelve_rows(self):
        output = self.render(self.crowd(8), size=(81, 12))
        lines = output.splitlines()
        self.assertEqual(len(lines), 12)
        self.assertNotIn("MORE", output)
        # The metadata line is the first thing dropped: it must hide nobody.
        self.assertTrue(lines[0].startswith("NAME"))
        self.assertIn("Crew7", output)

    def test_the_metadata_line_returns_when_a_row_is_spare(self):
        self.assertHeader(self.render(self.crowd(8), size=(81, 13)), 8)

    def test_overflow_folds_the_rest_into_one_more_row(self):
        output = self.render(self.crowd(8), size=(81, 8))
        lines = output.splitlines()
        self.assertEqual(len(lines), 8)
        more = self.line(output, "MORE(5)")
        self.assertIn("current; resize", more)
        self.assertIn("1.05M", more)
        self.assertIn("$0.65", more)
        # Hidden crew are counted once; TOTAL still covers everyone.
        self.assertIn("1.78M", self.line(output, "TOTAL "))

    def test_six_rows_keep_the_footer_and_hide_seven_crew(self):
        output = self.render(self.crowd(8), size=(81, 6))
        self.assertEqual(len(output.splitlines()), 6)
        self.assertIn("MORE(7)", output)
        self.assertTrue(output.splitlines()[-1].startswith("TOTAL is session-cumulative"))

    def test_the_full_frame_fits_eighty_columns(self):
        output = self.render(self.crowd(3), size=(80, 24))
        self.assertLessEqual(max(len(line) for line in output.splitlines()), 79)
        self.assertIn("codex/gpt-5.6-terra", output)

    def test_the_ctx_qualifier_is_dropped_with_the_bar_not_a_numeric_column(self):
        narrow = self.line(self.render(self.crowd(3), size=(46, 6)), "NAME")
        self.assertNotIn("CTX NOW", narrow)
        self.assertIn("CTX", narrow)
        self.assertIn("CUM TOK", narrow)
        self.assertIn("CUM $", narrow)

    def test_narrow_panes_alias_the_model_then_drop_the_bar_then_agent(self):
        usages = self.crowd(3)
        aliased = self.render(usages, size=(64, 6))
        self.assertIn("codex/terra", aliased)
        self.assertIn("#....", aliased)

        no_bar = self.render(usages, size=(52, 6))
        self.assertNotIn("#....", no_bar)
        self.assertIn("11%", no_bar)
        self.assertIn("$0.13", no_bar)

        compact = self.render(usages, size=(46, 6))
        self.assertNotIn("codex", compact)
        self.assertIn("CUM TOK", compact)
        self.assertIn("$/h", compact)

    def test_a_pane_too_narrow_for_the_numbers_states_the_total_instead(self):
        output = self.render(self.crowd(3), size=(30, 6))
        self.assertIn("widen pane", output)
        self.assertIn("TOTAL", output)
        self.assertNotIn("NAME", output)

    def test_the_footer_shortens_before_a_column_is_dropped(self):
        usages = self.crowd(3) | {"gibbs": usage(3_104_000, 0.56)}
        meta = memory.read_json(self.directory / "session.json")
        meta["crew"]["gibbs"] = {
            "id": "gibbs",
            "name": "Gibbs",
            "agent": "g",
            "status": "dismissed",
        }
        memory.write_json(self.directory / "session.json", meta)
        output = self.render(usages, size=(56, 6))
        self.assertEqual(
            output.splitlines()[-1], "Incl retired(1): 3.10M tok/$0.56; USD est; rounded"
        )
        # AGENT is clipped, not dropped: the footer paid first.
        self.assertIn("AGE~", output)
        self.assertIn("cod~", output)

    def test_an_annotation_too_long_to_fit_wraps_and_costs_a_roster_row(self):
        usages = self.crowd(3) | {"gibbs": usage(3_104_000, None)}
        meta = memory.read_json(self.directory / "session.json")
        meta["crew"]["gibbs"] = {
            "id": "gibbs",
            "name": "Gibbs",
            "agent": "g",
            "status": "dismissed",
        }
        memory.write_json(self.directory / "session.json", meta)
        output = self.render(usages, size=(46, 6))
        lines = output.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertIn("3.10M tok/$?", " ".join(lines[-2:]))
        self.assertIn("MORE(", output)


class FormattingTests(unittest.TestCase):
    def test_token_suffixes_carry_three_significant_digits(self):
        cases = {
            0: "0",
            999: "999",
            1_000: "1.00k",
            9_994: "9.99k",
            9_999: "10.0k",
            130_000: "130k",
            999_499: "999k",
            999_999: "1.00M",
            2_700_000: "2.70M",
            6_300_000: "6.30M",
            1_500_000_000: "1.50B",
            None: "-",
        }
        for value, expected in cases.items():
            self.assertEqual(dashboard._tokens(value), expected, value)

    def test_the_bar_uses_the_nearest_fifth_and_never_inflates_small_usage(self):
        cases = {
            7: ".....   7%",
            11: "#....  11%",
            31: "##...  31%",
            80: "####.  80%",
            100: "##### 100%",
        }
        for pct, expected in cases.items():
            self.assertEqual(dashboard._ctx(pct, frozenset()), expected)
        self.assertEqual(dashboard._ctx(None, frozenset()), "-")
        self.assertEqual(dashboard._ctx(7, frozenset({"bar"})), "  7%")


if __name__ == "__main__":
    unittest.main()
