"""What `wait` prints: the crew's report, or a pane tail with the TUI chrome gone."""

import contextlib
import io
import os
import unittest
from itertools import count
from unittest.mock import patch

from captain_barbossa import agents, cli, config, memory, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase


class WaitTailTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # nudge_block/nudge read a realistic agent status these ad hoc herdr fakes don't model;
        # a mail doorbell is not what these tests exercise, so give delivery a clean ring by
        # default. test_submit.py covers nudge_block/nudge themselves against real fakes.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def wait_crew_record(self, status="started"):
        agent_name = f"c-{self.meta['id'][:8]}-jack"
        self.meta["crew"] = {
            "jack": {
                "id": "jack",
                "name": "Jack",
                "agent": agent_name,
                "provider": "claude",
                "pane": "w1:p2",
                "tab": "w1:t1",
                "status": status,
            }
        }
        store.write_json(self.directory / "session.json", self.meta)
        return agent_name

    def wait_api(self, statuses, tail="", report=None):
        remaining = list(statuses)

        def api(*args, **kwargs):
            if args[:2] == ("agent", "read"):
                if not remaining:
                    return tail
                status = remaining.pop(0)
                if report and status in ("idle", "blocked"):
                    # The crew records its report just before its pane settles.
                    memory.add_memory(self.directory / "graph.json", "Jack", "report", report)
                if panes.modal_start(tail.splitlines()) is not None:
                    return tail
                if status == "blocked":
                    return tail + "\n1. Yes\n2. No\nPress enter to confirm or esc to cancel"
                return tail + ("\n❯" if status == "idle" else "\nworking")
            raise AssertionError(f"unexpected herdr call: {args}")

        return api

    def run_wait(self, statuses, tail="", timeout=60, report=None):
        with (
            patch.object(
                runtime, "herdr", side_effect=self.wait_api(statuses, tail, report)
            ) as calls,
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(
                self.args("wait", "Jack", "--timeout", str(timeout)), self.pane, self.project
            )
        return output.getvalue(), calls

    def completions(self):
        path = self.directory / "graph.json"
        if not path.exists():
            return []
        graph = store.read_json(path)
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        return [
            labels[link["target"]]
            for link in graph["links"]
            if link["relation"] == "completed" and labels[link["source"]] == "Jack"
        ]

    def tails(self):
        path = self.directory / "graph.json"
        if not path.exists():
            return []
        graph = store.read_json(path)
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        return [
            labels[link["target"]]
            for link in graph["links"]
            if link["relation"] == "tail" and labels[link["source"]] == "Jack"
        ]

    def test_wait_records_and_prints_the_report_the_crew_wrote(self):
        self.wait_crew_record()
        printed, _ = self.run_wait(["working", "idle", "idle", "idle"], report="tests pass")
        self.assertEqual(printed, "Jack idle.\nidle; reported: tests pass\n")
        self.assertEqual(self.completions(), ["idle; reported"])
        self.assertNotIn("pane tail", printed)

    def test_wait_prints_the_pane_tail_when_the_crew_wrote_no_report(self):
        agent_name = self.wait_crew_record()
        printed, calls = self.run_wait(["idle", "idle", "idle"], tail="  ran 66 tests\n\nOK\n")
        self.assertIn("no report recorded; pane tail: ran 66 tests\nOK", printed)
        self.assertEqual(self.completions(), ["idle; no report recorded"])
        # The tail is printed, never stored: memory carries reports, not terminal scrollback.
        self.assertEqual(self.tails(), [])
        read = [call for call in calls.call_args_list if call.args[:2] == ("agent", "read")]
        self.assertEqual(read[0].args, ("agent", "read", agent_name, "--lines", "40"))
        self.assertTrue(read[0].kwargs["raw"])

    def test_wait_strips_claude_code_tui_chrome_from_the_pane_tail(self):
        # A real `herdr agent read` capture of an idle Claude Code pane: prose, a
        # spinner tagline, the prompt box (rule, non-empty prompt, rule), and the
        # bottom status bar.
        rule = "─" * 89
        tail = "\n".join(
            [
                "Main is at 2bca137 with version 0.9.0, one commit ahead of origin and still",
                "unpushed.",
                "",
                "✻ Worked for 35s · done 2:02 AM · 1 shell still running",
                "",
                rule,
                "❯ push it once Jack is done",
                rule,
                "  ⏵⏵ auto mode on · 1 shell · ← for agents · ↓ to manage",
            ]
        )
        self.wait_crew_record()
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail=tail)
        entry = "pane tail: " + "\n".join(
            [
                "Main is at 2bca137 with version 0.9.0, one commit ahead of origin and still",
                "unpushed.",
                "✻ Worked for 35s · done 2:02 AM · 1 shell still running",
                "❯ push it once Jack is done",
            ]
        )
        self.assertIn(entry, printed)
        self.assertNotIn("mode on", printed)
        self.assertNotIn(rule, printed)

    def test_wait_strips_codex_tui_chrome_from_the_pane_tail(self):
        # A real `herdr agent read` capture of an idle Codex pane: a completion
        # line, a rule, a "recap" header rule, prose, the empty prompt placeholder,
        # and the bottom status bar.
        rule = "─" * 108
        tail = "\n".join(
            [
                "• Lean already. Ship.",
                "",
                rule,
                "",
                "─ Conversation recap " + "─" * 88,
                "",
                "  Timestamp fields were updated to UTC Unix epoch milliseconds using PostgreSQL "
                "bigint and TypeScript",
                "  numbers. The Ponytail review is complete and found the changes lean and ready to ship.",
                "",
                "",
                "› Ask Codex to do anything",
                "",
                "  gpt-6-astra high · ~/code/projects/hrly",
            ]
        )
        self.wait_crew_record()
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail=tail)
        self.assertIn("pane tail: • Lean already. Ship.", printed)
        self.assertIn("Conversation recap", printed)
        self.assertIn("Timestamp fields were updated", printed)
        self.assertNotIn("Ask Codex to do anything", printed)
        self.assertNotIn("gpt-6-astra", printed)
        self.assertNotIn(rule, printed)

    def rate_limit_modal(self):
        """A real `herdr agent read` capture of a Codex pane that Herdr reports idle."""
        return "\n".join(
            [
                "• Ran uv run --locked python -m unittest -q (with UV_CACHE_DIR=/tmp/uv-cache to avoid",
                "a local permission issue).",
                "77 tests ran, all passed (OK).",
                "Approaching rate limits",
                "Switch to gpt-5.6-luna for lower credit usage?",
                "› 1. Switch to gpt-5.6-luna                 Fast and affordable agentic coding",
                "model.",
                "2. Keep current model",
                "3. Keep current model (never show again)  Hide future rate limit reminders about",
                "switching models.",
                "Press enter to confirm or esc to go back",
            ]
        )

    def test_wait_reports_a_rate_limit_modal_as_blocked_not_completed(self):
        self.wait_crew_record()
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail=self.rate_limit_modal())
        self.assertIn("Jack blocked.", printed)
        self.assertIn("blocked; no report recorded", printed)
        self.assertEqual(len(self.completions()), 1)
        self.assertTrue(self.completions()[0].startswith("blocked;"))

    def test_wait_keeps_a_choice_modals_question_and_drops_only_its_options(self):
        self.wait_crew_record()
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail=self.rate_limit_modal())
        entry = "pane tail: " + "\n".join(
            [
                "• Ran uv run --locked python -m unittest -q (with UV_CACHE_DIR=/tmp/uv-cache to avoid",
                "a local permission issue).",
                "77 tests ran, all passed (OK).",
                "Approaching rate limits",
                "Switch to gpt-5.6-luna for lower credit usage?",
            ]
        )
        self.assertIn(entry, printed)
        for text in ("1. Switch to gpt-5.6-luna", "Keep current model", "Press enter to confirm"):
            self.assertNotIn(text, printed)

    def test_a_numbered_list_in_crew_output_is_not_read_as_a_modal(self):
        tail = "\n".join(
            [
                "⏺ Remaining work:",
                "1. Bump the version",
                "2. Run the gate",
                "Press enter to continue reading the diff",
            ]
        )
        self.wait_crew_record()
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail=tail)
        self.assertIn("Jack idle.", printed)
        self.assertIn("1. Bump the version", printed)

    def test_wait_delivers_an_unconsumed_report_once_and_not_again(self):
        self.wait_crew_record()
        memory.add_memory(self.directory / "graph.json", "Jack", "report", "previous run")
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail="waiting")
        self.assertIn("reported: previous run", printed)
        printed, _ = self.run_wait(["idle", "idle", "idle"], tail="waiting")
        self.assertIn("no report recorded; pane tail: waiting", printed)
        self.assertNotIn("previous run", printed)

    def test_wait_fallback_settles_only_after_consecutive_idle_pane_reads(self):
        self.wait_crew_record()
        printed, calls = self.run_wait(["idle", "idle", "working", "idle", "idle", "idle"], "tail")
        self.assertIn("Jack idle.", printed)
        self.assertEqual(
            len([c for c in calls.call_args_list if c.args[:2] == ("agent", "read")]), 7
        )

    def test_wait_reports_a_blocked_crew_with_its_pane_tail(self):
        self.wait_crew_record()
        printed, _ = self.run_wait(
            ["working", "blocked"], tail="Do you want to proceed?", report="partial"
        )
        self.assertIn("Jack blocked.", printed)
        self.assertIn("reported: partial", printed)
        self.assertIn("pane tail: Do you want to proceed?", printed)
        # The tail is shown to the captain but not persisted, since a report already was.
        self.assertEqual(self.completions(), ["blocked; reported"])
        self.assertEqual(self.tails(), [])

    def test_wait_times_out_without_recording_a_completion(self):
        self.wait_crew_record()
        with (
            patch.object(runtime, "herdr", side_effect=self.wait_api(["working"])),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "still working after 1.0 seconds"):
                agents.wait_crew(
                    self.args("wait", "Jack", "--timeout", "1"), self.pane, self.project
                )
        self.assertEqual(self.completions(), [])

    def test_an_omitted_timeout_falls_back_to_the_crew_setting(self):
        """The parser leaves --timeout unset, so wait itself reads [crew] wait_timeout."""
        self.wait_crew_record()
        seen = []
        with (
            patch.object(
                agents.Crew,
                "status",
                lambda _self, timeout: seen.append(timeout) or ("idle", None),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.wait_crew(self.args("wait", "Jack"), self.pane, self.project)
        self.assertEqual(seen, [config.lookup("crew", "wait_timeout", kind=int)])

    def test_a_negative_timeout_is_refused_rather_than_quietly_clamped(self):
        """0 still means "report what has already arrived"; below that is a mistake."""
        self.wait_crew_record()
        with patch.object(runtime, "herdr", side_effect=AssertionError("polled")):
            with self.assertRaisesRegex(runtime.CaptainError, "--timeout must be a number"):
                agents.wait_crew(
                    self.args("wait", "Jack", "--timeout", "-5"), self.pane, self.project
                )

    def test_wait_rejects_unknown_crew_before_polling_herdr(self):
        self.wait_crew_record()
        with patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(runtime.CaptainError, "No crew named 'Gibbs'"):
                agents.wait_crew(self.args("wait", "Gibbs"), self.pane, self.project)
        api.assert_not_called()

    def test_wait_survives_an_unreadable_pane(self):
        self.wait_crew_record()

        def api(*args, **kwargs):
            if args[:2] == ("agent", "read"):
                raise runtime.CaptainError("pane closed")
            return {"agent": {"agent_status": "idle"}}

        with (
            patch.object(runtime, "herdr", side_effect=api),
            patch.object(Crew, "status", return_value=("idle", None)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(self.args("wait", "Jack"), self.pane, self.project)
        self.assertIn("pane tail: unreadable (pane closed)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
