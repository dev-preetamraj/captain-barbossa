"""What a single-crew `wait` reports from a codex pane, and how it reads its arguments."""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, config, protocol, runtime, sessions, store
from captain_barbossa.crew import Crew

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase


class WaitTailTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "pane": "w1:p2",
                "incarnation_id": "first",
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        self.assignment = protocol.begin(self.crew, self.project, "build", ["src"], ["edit"])

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.current.meta["id"], *args])

    def run_wait(self, screen):
        """One bounded look at a pane Herdr reports idle, as `wait Jack --timeout 0`."""

        def api(*args, **kwargs):
            if args[:2] == ("agent", "read"):
                return screen
            raise AssertionError(f"unexpected herdr call: {args}")

        with (
            patch.object(runtime, "herdr", side_effect=api),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.wait_crew(
                self.args("wait", "Jack", "--json", "--timeout", "0"), self.pane, self.project
            )
        return json.loads(output.getvalue())

    def saved(self):
        return store.read_json(self.current.directory / "protocol.json")["assignments"][
            self.assignment["id"]
        ]

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
        result = self.run_wait(self.rate_limit_modal())
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertIn("Switch to gpt-5.6-luna", result["summary"])
        self.assertTrue(result["delivery_id"])
        self.assertEqual(self.saved()["state"], "working")

    def test_a_numbered_list_in_crew_output_is_not_read_as_a_modal(self):
        tail = "\n".join(
            [
                "⏺ Remaining work:",
                "1. Bump the version",
                "2. Run the gate",
                "Press enter to continue reading the diff",
            ]
        )
        result = self.run_wait(tail)
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["delivery_id"])

    def test_wait_times_out_without_recording_a_completion(self):
        result = self.run_wait("› Ask Codex to do anything")
        self.assertEqual((result["status"], result["delivery_id"]), ("timeout", None))
        saved = self.saved()
        self.assertEqual(
            (saved["state"], saved["notices"], saved["pending"]), ("working", [], None)
        )

    def test_an_omitted_timeout_falls_back_to_the_crew_setting(self):
        """The parser leaves --timeout unset, so wait itself reads [crew] wait_timeout."""
        seen = []

        def wait(crew, timeout, ack):
            seen.append(timeout)
            return protocol.result(crew, None, "idle")

        with patch.object(protocol, "wait", wait), contextlib.redirect_stdout(io.StringIO()):
            agents.wait_crew(self.args("wait", "Jack"), self.pane, self.project)
        self.assertEqual(seen, [config.lookup("crew", "wait_timeout", kind=int)])

    def test_a_negative_timeout_is_refused_rather_than_quietly_clamped(self):
        """0 still means "report what has already arrived"; below that is a mistake."""
        with patch.object(runtime, "herdr", side_effect=AssertionError("polled")):
            with self.assertRaisesRegex(runtime.CaptainError, "--timeout must be a number"):
                agents.wait_crew(
                    self.args("wait", "Jack", "--timeout", "-5"), self.pane, self.project
                )

    def test_wait_rejects_unknown_crew_before_polling_herdr(self):
        with patch.object(runtime, "herdr") as api:
            with self.assertRaisesRegex(runtime.CaptainError, "No crew named 'Gibbs'"):
                agents.wait_crew(self.args("wait", "Gibbs"), self.pane, self.project)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
