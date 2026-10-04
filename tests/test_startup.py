"""Crew startup: settling the native CLI, then the first ring and its durable mail."""

import contextlib
import io
import os
import unittest
from itertools import count, repeat
from unittest.mock import patch

from captain_barbossa import agents, cli, runtime, sessions, store
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase

# create_crew's shell_ready_for_input polls a raw `pane read`, which needs text; a
# fixture built to return a dict for every herdr call would otherwise blow up on it.
SETTLED_SHELL_TEXT = "~/project $ "


# Real implementations, captured before StartupTests.setUp patches the class default,
# so a test can restore actual gate/ring behavior against its own herdr fake.
REAL_NUDGE_BLOCK = Pane.nudge_block
REAL_NUDGE = Pane.nudge


def pane_stub(base):
    """Wrap a herdr stub so a `pane read` returns settled shell text instead of
    whatever `base` answers everything else with (a dict, `base` being callable or not)."""

    def api(*call, **kwargs):
        if call[:2] == ("pane", "read"):
            return SETTLED_SHELL_TEXT
        return base(*call, **kwargs) if callable(base) else base

    return api


class StartupTests(SessionCase):
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

    EMPTY_COMPOSER = "› Ask Codex to do anything"

    def test_startup_failure_preserves_pane_and_prevents_duplicate_retry(self):
        args = self.args(
            "crew",
            "jack",
            "--agent",
            "codex",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )

        def api(*args, **kwargs):
            if args[:2] == ("pane", "run"):
                raise runtime.CaptainError("agent_not_ready")
            if args[:2] == ("pane", "read"):
                return SETTLED_SHELL_TEXT
            return {"pane": {"pane_id": "w1:p2"}}

        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/codex"),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "pane was preserved"):
                agents.create_crew(args, self.pane, self.project)
            self.assertFalse(
                any(call.args[:2] == ("pane", "close") for call in calls.call_args_list)
            )
            self.assertFalse(
                any(call.args[:2] == ("agent", "prompt") for call in calls.call_args_list)
            )
            with self.assertRaisesRegex(runtime.CaptainError, "already exists"):
                agents.create_crew(args, self.pane, self.project)
        saved = store.read_json(self.directory / "session.json")
        self.assertEqual(saved["crew"]["jack"]["status"], "needs_attention")
        # A launcher that deleted itself made a failed startup unrecoverable; it now
        # survives, and dismiss_crew is what eventually unlinks crew-<id>.sh.
        self.assertTrue((self.directory / "crew-jack.sh").exists())

    def test_startup_waits_for_the_expected_native_agent(self):
        states = [
            {},
            {"agent": "claude", "agent_status": "idle"},
            {"agent": "codex", "agent_status": "working"},
            {"agent": "codex", "agent_status": "done"},
        ]

        def api(*args, **kwargs):
            return (
                {"pane": states.pop(0)}
                if args[:2] == ("pane", "get")
                else {"agent": {"name": "builder"}}
            )

        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(panes.time, "sleep"),
        ):
            Pane("builder").wait_for_crew("w1:p2", "codex")
        self.assertEqual(states, [])
        self.assertEqual(calls.call_args_list[-2].args, ("agent", "rename", "w1:p2", "builder"))
        self.assertEqual(calls.call_args_list[-1].args, ("agent", "get", "w1:p2"))

    def test_startup_waits_for_a_settled_idle_cli_before_prompting(self):
        states = [
            {"agent": "claude", "agent_status": "idle"},
            {"agent": "claude", "agent_status": "working"},
            {"agent": "claude", "agent_status": "idle"},
            {"agent": "claude", "agent_status": "idle"},
        ]

        def api(*call, **kwargs):
            if call[:2] == ("pane", "get"):
                return {"pane": states.pop(0)}
            return {"agent": {"name": "builder"}}

        with (
            patch.object(panes, "READY_POLLS", 2),
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(panes.time, "sleep") as sleep,
        ):
            Pane("builder").wait_for_crew("w1:p2", "claude")
        self.assertEqual(states, [])
        self.assertEqual(calls.call_args_list[-2].args, ("agent", "rename", "w1:p2", "builder"))
        self.assertEqual(sleep.call_count, 3)

    def test_startup_rejects_unverified_agent_name(self):
        for response in ({"agent": {"name": "other"}}, {"agent": {}}, {}):
            with (
                self.subTest(response=response),
                patch.object(
                    runtime,
                    "herdr",
                    side_effect=[
                        {"pane": {"agent": "codex", "agent_status": "idle"}},
                        {},
                        response,
                    ],
                ) as api,
                patch.object(panes.time, "sleep") as sleep,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "rename failed for pane w1:p2"):
                    Pane("builder").wait_for_crew("w1:p2", "codex")
                self.assertEqual(
                    [call.args for call in api.call_args_list[-2:]],
                    [("agent", "rename", "w1:p2", "builder"), ("agent", "get", "w1:p2")],
                )
                sleep.assert_not_called()

    def test_blocked_or_timed_out_startup_never_submits_the_task(self):
        for status, name in (("blocked", "jack"), ("unknown", "gibbs")):
            with self.subTest(status=status):
                args = self.args(
                    "crew",
                    name,
                    "--agent",
                    "codex",
                    "--task",
                    "build",
                    "--placement",
                    "pane",
                    "--direction",
                    "vertical",
                    "--split-pane",
                    "w1:p1",
                )
                created = {"pane": {"pane_id": "w1:p2", "agent": "codex", "agent_status": status}}
                created["agent"] = {"name": f"c-{self.meta['id'][:8]}-{name}"}
                with (
                    patch.object(runtime, "herdr", side_effect=pane_stub(created)) as calls,
                    patch.object(agents, "executable", return_value="/bin/codex"),
                    # One extra tick: shell_ready_for_input's own deadline check runs
                    # (and settles at once) before wait_for_crew consumes [0, 1, 31].
                    patch.object(panes.time, "monotonic", side_effect=[0, 0, 1, 31]),
                    patch.object(panes.time, "sleep"),
                ):
                    message = "input or approval" if status == "blocked" else "did not become ready"
                    with self.assertRaisesRegex(runtime.CaptainError, message):
                        agents.create_crew(args, self.pane, self.project)
                self.assertFalse(
                    any(call.args[:2] == ("agent", "prompt") for call in calls.call_args_list)
                )
                self.assertFalse(
                    any(call.args[:2] == ("pane", "close") for call in calls.call_args_list)
                )
                saved = store.read_json(self.directory / "session.json")["crew"][name]
                self.assertEqual(saved["status"], "needs_attention")

    def crew_status_api(self, statuses):
        agent_name = f"c-{self.meta['id'][:8]}-jack"

        def api(*call, **kwargs):
            if call[:2] == ("agent", "get") and call[2] == agent_name:
                return {"agent": {"name": agent_name, "agent_status": next(statuses)}}
            if call[:2] == ("agent", "read"):
                return self.EMPTY_COMPOSER
            return {
                "pane": {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"},
                "agent": {"name": agent_name},
            }

        return agent_name, pane_stub(api)

    def test_slow_status_transitions_never_delay_the_durable_initial_mail(self):
        """The initial task is mailed and rung once; it no longer polls for landing."""
        args = self.args(
            "crew",
            "jack",
            "--agent",
            "claude",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        agent_name, api = self.crew_status_api(iter(["idle", "idle", "working"]))
        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK),
            patch.object(Pane, "nudge", REAL_NUDGE),
        ):
            agents.create_crew(args, self.pane, self.project)
        sent = [call.args[:2] for call in calls.call_args_list]
        self.assertEqual(sent.count(("agent", "prompt")), 1)
        self.assertNotIn(("agent", "send-keys"), sent)
        saved = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(saved["status"], "started")

    def test_a_settled_idle_composer_delivers_durable_mail_once(self):
        """A clean idle composer at delivery time still rings exactly once."""
        args = self.args(
            "crew",
            "jack",
            "--agent",
            "claude",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        agent_name, api = self.crew_status_api(repeat("idle"))
        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK),
            patch.object(Pane, "nudge", REAL_NUDGE),
        ):
            agents.create_crew(args, self.pane, self.project)
        sent = [call.args[:2] for call in calls.call_args_list]
        self.assertEqual(sent.count(("agent", "prompt")), 1)
        self.assertNotIn(("agent", "send-keys"), sent)
        saved = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(saved["status"], "started")

    def test_a_blocked_agent_at_delivery_time_holds_the_ring_without_typing(self):
        """A pending approval prompt holds the doorbell; the mail is still delivered."""
        args = self.args(
            "crew",
            "jack",
            "--agent",
            "claude",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        agent_name, api = self.crew_status_api(repeat("blocked"))
        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK),
            patch.object(Pane, "nudge", REAL_NUDGE),
        ):
            agents.create_crew(args, self.pane, self.project)
        sent = [call.args[:2] for call in calls.call_args_list]
        # A held gate never rings; the crew reads its mail once the approval clears.
        self.assertNotIn(("agent", "prompt"), sent)
        self.assertNotIn(("agent", "send-keys"), sent)
        saved = store.read_json(self.directory / "session.json")["crew"]["jack"]
        self.assertEqual(saved["status"], "started")

    def test_our_own_ring_echo_does_not_gate_the_next_ring(self):
        """A composer still showing the ring we typed is ours; anything else still gates."""
        crew = Crew(
            "jack",
            {"name": "Jack", "agent": "c-session-jack", "provider": "codex"},
            sessions.Session(self.directory, self.meta),
        )
        echo = panes.inbox_line(crew)
        for screen, gate in (
            (f"› {echo}", None),
            (f"› {echo[:-4]}", panes.DRAFT_GATE),
            ("› half a sentence", panes.DRAFT_GATE),
            ("› Ask Codex to do anything", None),
        ):
            with self.subTest(screen=screen[:40]):

                def api(*call, screen=screen, **kwargs):
                    if call[:2] == ("agent", "get"):
                        return {"agent": {"name": "c-session-jack", "agent_status": "idle"}}
                    return screen

                with (
                    patch.object(runtime, "herdr", side_effect=api),
                    patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK),
                ):
                    self.assertEqual(crew.pane.nudge_block(crew), gate)

    def test_a_done_status_crew_is_as_ringable_as_an_idle_one(self):
        """Herdr reports a crew that just finished a turn as done, not idle."""
        args = self.args(
            "crew",
            "jack",
            "--agent",
            "claude",
            "--task",
            "build",
            "--placement",
            "pane",
            "--direction",
            "vertical",
            "--split-pane",
            "w1:p1",
        )
        agent_name, api = self.crew_status_api(repeat("done"))
        with (
            patch.object(runtime, "herdr", side_effect=api) as calls,
            patch.object(agents, "executable", return_value="/bin/claude"),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            patch.object(Pane, "nudge_block", REAL_NUDGE_BLOCK),
            patch.object(Pane, "nudge", REAL_NUDGE),
        ):
            agents.create_crew(args, self.pane, self.project)
            crew = Crew(
                "jack",
                store.read_json(self.directory / "session.json")["crew"]["jack"],
                sessions.Session(self.directory, self.meta),
            )
            self.assertIsNone(crew.pane.nudge_block(crew))
        sent = [call.args[:2] for call in calls.call_args_list]
        self.assertEqual(sent.count(("agent", "prompt")), 1)
        self.assertNotIn(("agent", "send-keys"), sent)

    def test_done_or_working_agent_after_prompt_is_confirmed_without_enter(self):
        for status in ("done", "working"):
            with (
                self.subTest(status=status),
                patch.object(
                    runtime,
                    "herdr",
                    return_value={"agent": {"name": "builder", "agent_status": status}},
                ) as api,
                patch.object(Pane, "lines", return_value=["❯"]),
                patch.object(Pane, "draft_pending", return_value=False),
                patch.object(panes.time, "sleep") as sleep,
            ):
                Pane("builder").submit_task("build", "claude")
                self.assertEqual(
                    [call.args for call in api.call_args_list[1:]],
                    [("agent", "prompt", "builder", "build"), ("agent", "get", "builder")],
                )
                sleep.assert_not_called()

    def test_post_submit_block_or_unknown_needs_attention_without_enter(self):
        for statuses, message in (
            (["idle", "idle", "blocked"], "outcome is unknown"),
            (["idle", "idle", "idle", None], "outcome is unknown"),
        ):
            with (
                self.subTest(statuses=statuses),
                patch.object(
                    runtime,
                    "herdr",
                    side_effect=lambda *call, statuses=iter(statuses), **kwargs: {
                        "agent": {
                            "name": "builder",
                            "agent_status": next(statuses, None)
                            if call[:2] == ("agent", "get")
                            else "idle",
                        }
                    },
                ) as api,
                patch.object(Pane, "lines", return_value=["❯"]),
                patch.object(Pane, "draft_pending", return_value=False),
                patch.object(panes.time, "sleep"),
                patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
            ):
                with self.assertRaisesRegex(runtime.CaptainError, message):
                    Pane("builder").submit_task("build", "claude")
                sent = [
                    call.args
                    for call in api.call_args_list
                    if call.args[:2] == ("agent", "send-keys")
                ]
                self.assertEqual(sent, [])

    def test_unknown_status_after_prompt_never_receives_enter(self):
        with (
            patch.object(runtime, "herdr", return_value={"agent": {"name": "builder"}}) as api,
            patch.object(Pane, "lines", return_value=["❯"]),
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "no task was sent"):
                Pane("builder").submit_task("build", "claude")
        self.assertFalse(
            any(call.args[:2] == ("agent", "send-keys") for call in api.call_args_list)
        )

    def prompt_api(self, statuses, tail=""):
        def api(*call, **kwargs):
            if call[:2] == ("agent", "read"):
                return tail
            return {"agent": {"name": "builder", "agent_status": next(statuses, None)}}

        return api

    def test_a_codex_choice_modal_after_a_prompt_needs_attention_without_enter(self):
        with (
            patch.object(
                runtime,
                "herdr",
                side_effect=self.prompt_api(repeat("idle"), self.rate_limit_modal()),
            ) as api,
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "waiting for input or approval"):
                Pane("builder").submit_task("build", "codex")
        self.assertFalse(
            any(call.args[:2] == ("agent", "send-keys") for call in api.call_args_list)
        )

    def test_an_idle_codex_pane_never_receives_enter_or_resend(self):
        with (
            patch.object(runtime, "herdr", side_effect=self.prompt_api(repeat("idle"), "❯")) as api,
            patch.object(panes.time, "sleep"),
            patch.object(panes.time, "monotonic", side_effect=count(0, 2)),
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "outcome is unknown"):
                Pane("builder").submit_task("build", "codex")
        sent = [call.args[:2] for call in api.call_args_list]
        self.assertEqual(sent.count(("agent", "prompt")), 1)
        self.assertEqual(sent.count(("agent", "send-keys")), 0)

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


if __name__ == "__main__":
    unittest.main()
