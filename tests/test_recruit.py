"""Recruiting a crew: the selections it requires, and what it creates once answered."""

import contextlib
import io
import json
import os
import pty
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, models, runtime, sessions, store
from captain_barbossa import instructions as instruction_prompts
from captain_barbossa import pane as panes
from captain_barbossa.crew import Crew
from captain_barbossa.pane import Pane

# Imported for its side effect: HOME and the captain memory roots are temp directories
# for every test in this process. tests/test_isolation.py guards it.
from tests.home_isolation import HERDR, SessionCase

# create_crew's shell_ready_for_input polls a raw `pane read`, which needs text; a
# fixture built to return a dict for every herdr call would otherwise blow up on it.
SETTLED_SHELL_TEXT = "~/project $ "


def pane_stub(base):
    """Wrap a herdr stub so a `pane read` returns settled shell text instead of
    whatever `base` answers everything else with (a dict, `base` being callable or not)."""

    def api(*call, **kwargs):
        if call[:2] == ("pane", "read"):
            return SETTLED_SHELL_TEXT
        return base(*call, **kwargs) if callable(base) else base

    return api


class RecruitTests(SessionCase):
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

    def test_captain_requires_an_agent_selection(self):
        for provider in ("claude", "codex"):
            with (
                self.subTest(provider=provider),
                patch.object(runtime, "herdr"),
                patch.object(agents, "executable", side_effect=lambda name: f"/bin/{name}"),
                patch.object(os, "execvpe") as execute,
                patch.object(sys.stdin, "isatty", return_value=True),
                patch(
                    "captain_barbossa.prompts.questionary.select",
                    **{"return_value.unsafe_ask.return_value": provider},
                ) as ask,
            ):
                agents.launch(self.args(), self.pane, self.project)
                self.assertEqual(execute.call_args.args[0], f"/bin/{provider}")
                self.assertIn("Choose your captain", ask.call_args.args[0])
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch(
                "captain_barbossa.prompts.questionary.select",
                **{"return_value.unsafe_ask.return_value": None},
            ),
            patch.object(runtime, "herdr") as api,
        ):
            with self.assertRaisesRegex(runtime.CaptainError, "cancelled"):
                agents.launch(self.args(), self.pane, self.project)
            api.assert_not_called()

    def test_crew_requires_choices_and_cancellation_creates_nothing(self):
        for flags in (
            (),
            ("--agent", "codex"),
            ("--placement", "pane"),
            ("--agent", "codex", "--placement", "pane"),
        ):
            args = self.args("crew", "--task", "build", *flags)
            with (
                self.subTest(flags=flags),
                patch.object(sys.stdin, "isatty", return_value=False),
                patch.object(runtime, "herdr") as api,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "Ask the user"):
                    agents.create_crew(args, self.pane, self.project)
                api.assert_not_called()
        for answers in ([None], ["claude", None]):
            with (
                patch.object(sys.stdin, "isatty", return_value=True),
                patch(
                    "captain_barbossa.prompts.questionary.select",
                    **{"return_value.unsafe_ask.side_effect": answers},
                ),
                patch.object(runtime, "herdr") as api,
            ):
                with self.assertRaisesRegex(runtime.CaptainError, "cancelled"):
                    agents.create_crew(
                        self.args("crew", "--task", "build"), self.pane, self.project
                    )
                api.assert_not_called()

    def pane_list(self):
        return {
            "panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1", "terminal_title_stripped": "zsh"},
                {"pane_id": "w1:p5", "tab_id": "w1:t1", "label": "Will", "agent": "claude"},
                {"pane_id": "w1:p9", "tab_id": "w1:t2", "terminal_title_stripped": "vim"},
                {"pane_id": "w1:p8", "tab_id": "w1:t3"},
                {"pane_id": None, "tab_id": "w1:t1"},
                {"pane_id": "w1:p7", "tab_id": None},
                "junk",
            ]
        }

    def tab_list(self):
        return {
            "tabs": [
                {"tab_id": "w1:t1", "label": "Captain Barbossa", "number": 1},
                {"tab_id": "w1:t2", "number": 2},
                {"tab_id": None, "label": "ghost"},
            ]
        }

    def listing(self, *call, **_):
        if call[:2] == ("tab", "list"):
            return self.tab_list()
        if call[:2] == ("pane", "list"):
            return self.pane_list()
        if call[:2] == ("agent", "read"):
            return self.EMPTY_COMPOSER
        return None

    EMPTY_COMPOSER = "› Ask Codex to do anything"

    LISTING_CALLS = (("tab", "list", "--workspace", "w1"), ("pane", "list", "--workspace", "w1"))

    def test_crew_creates_chosen_topology_then_starts_native_agent(self):
        for placement, provider, name, display_name in (
            ("pane", "codex", "jack", "Jack"),
            ("tab", "claude", "will", "Will"),
        ):
            with self.subTest(placement=placement):
                task = 'Check quotes " and $() and `backticks`\nThen report.'
                args = self.args("crew", "--task", task)
                created = {
                    "pane": {"pane_id": "w1:p2", "agent": provider, "agent_status": "idle"},
                    "root_pane": {"pane_id": "w1:p3"},
                    "tab_id": "w1:t9",
                    "agent": {
                        "name": f"c-{self.meta['id'][:8]}-{name}",
                        "agent_status": "working",
                    },
                }
                with (
                    patch.object(
                        runtime,
                        "herdr",
                        side_effect=pane_stub(lambda *call, **_: self.listing(*call) or created),
                    ) as api,
                    patch.object(agents, "executable", return_value=f"/bin/{provider}"),
                    patch.object(sys.stdin, "isatty", return_value=True),
                    patch(
                        "captain_barbossa.prompts.questionary.select",
                        **{
                            "return_value.unsafe_ask.side_effect": [provider, placement]
                            + (["vertical", "w1:p1"] if placement == "pane" else [])
                        },
                    ) as ask,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    agents.create_crew(args, self.pane, self.project)
                result = json.loads(output.getvalue())
                self.assertEqual(result["name"], display_name)
                self.assertEqual(result["status"], "started")
                self.assertNotIn("task", result)
                # The composer read that confirms the task left the input box is not topology.
                calls = [call for call in api.call_args_list if call.args[:2] != ("agent", "read")]
                self.assertIn("Choose your crew agent", ask.call_args_list[0].args[0])
                self.assertIn("Where should the crew open?", ask.call_args_list[1].args[0])
                if placement == "pane":
                    self.assertIn("Split direction?", ask.call_args_list[2].args[0])
                    self.assertIn("Which pane should be split?", ask.call_args_list[3].args[0])
                    self.assertEqual([call.args for call in calls[:2]], list(self.LISTING_CALLS))
                    calls = calls[2:]
                    self.assertEqual(calls[0].args[:2], ("pane", "split"))
                    self.assertEqual(calls[0].args[calls[0].args.index("--pane") + 1], "w1:p1")
                    self.assertEqual(calls[0].args[calls[0].args.index("--direction") + 1], "right")
                    self.assertEqual(result["direction"], "vertical")
                    self.assertEqual(result["split_pane"], "w1:p1")
                    self.assertEqual(result["tab"], "w1:t1")
                else:
                    self.assertEqual(len(ask.call_args_list), 2)
                    self.assertEqual(calls[0].args[:2], ("tab", "create"))
                    self.assertIsNone(result["direction"])
                    self.assertEqual(result["tab"], "w1:t9")
                self.assertIn(f"CAPTAIN_SESSION={self.meta['id']}", calls[0].args)
                run = next(call for call in calls if call.args[:2] == ("pane", "run"))
                self.assertEqual(run.args[-1], '/bin/sh "$CAPTAIN_CREW_LAUNCHER"')
                self.assertEqual(run.kwargs, {"expect_output": False})
                # The task is mailed, not typed; the fake agent status ("working") also
                # holds the doorbell, so no "agent prompt" call carries it either.
                self.assertNotIn(("agent", "prompt"), [call.args[:2] for call in calls])
                (mail_path,) = (self.directory / "mail" / name).glob("*.json")
                mail = json.loads(mail_path.read_text())
                self.assertEqual(mail["text"], task)
                self.assertNotIn(result["assignment_id"], mail["text"])
                self.assertEqual(
                    [call.args[:2] for call in calls].count(("agent", "send-keys")),
                    0,
                )
                saved = store.read_json(self.directory / "session.json")["crew"][name]
                self.assertEqual(saved["task"], task)
                self.assertEqual(saved["id"], name)
                self.assertEqual(saved["name"], display_name)
                self.assertEqual(saved["agent"], f"c-{self.meta['id'][:8]}-{name}")
                self.assertEqual(saved["status"], "started")
                self.assertEqual(saved["provider"], provider)
                self.assertIn(
                    ("pane", "rename", saved["pane"], display_name), [call.args for call in calls]
                )
                if placement == "tab":
                    self.assertEqual(
                        calls[0].args[calls[0].args.index("--label") + 1], display_name
                    )
                launcher = self.directory / f"crew-{name}.sh"
                self.assertIn(f"crew member {display_name}", launcher.read_text())
                graph = store.read_json(self.directory / "graph.json")
                self.assertIn(display_name, [node["label"] for node in graph["nodes"]])
                self.assertIn(task, [node["label"] for node in graph["nodes"]])
                self.assertIn(f"CAPTAIN_CREW_LAUNCHER={launcher}", calls[0].args)
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(self.project.iterdir()), [])

    def test_long_native_instructions_survive_canonical_terminal_input(self):
        native = self.root / "fake agent's cli"
        received = self.root / "received.json"
        capture = "import json, os, sys; from pathlib import Path; Path(os.environ['CAPTAIN_TEST_ARGS']).write_text(json.dumps(sys.argv[1:]))"
        native.write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", capture]) + ' "$@"\n'
        )
        native.chmod(0o700)
        for provider, name in (("codex", "jack"), ("claude", "gibbs")):
            with self.subTest(provider=provider):
                instructions = (
                    "Long instructions: " + "quotes ' \" `false` $(false) \\ and newlines\n" * 100
                )
                args = self.args(
                    "crew",
                    name,
                    "--agent",
                    provider,
                    "--task",
                    "check",
                    "--placement",
                    "pane",
                    "--direction",
                    "vertical",
                    "--split-pane",
                    "w1:p1",
                )
                created = {"pane": {"pane_id": "w1:p2", "agent": provider, "agent_status": "idle"}}
                created["agent"] = {"name": f"c-{self.meta['id'][:8]}-{name}"}
                with (
                    patch.object(runtime, "herdr", side_effect=pane_stub(created)) as api,
                    patch.object(agents, "executable", return_value=str(native)),
                    patch.object(
                        instruction_prompts, "agent_instructions", return_value=instructions
                    ),
                    patch.object(Pane, "submit_task"),
                ):
                    agents.create_crew(args, self.pane, self.project)
                command = next(
                    call.args[-1] for call in api.call_args_list if call.args[:2] == ("pane", "run")
                )
                env = dict(
                    os.environ,
                    CAPTAIN_CREW_LAUNCHER=str(self.directory / f"crew-{name}.sh"),
                    CAPTAIN_TEST_ARGS=str(received),
                )
                master, slave = pty.openpty()
                try:
                    self.assertLess(len(command.encode()), os.fpathconf(slave, "PC_MAX_CANON"))
                    # read waits in canonical mode, as a newly opened terminal can do.
                    with subprocess.Popen(
                        ["/bin/sh", "-c", 'IFS= read -r command; eval "$command"'],
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        env=env,
                    ) as process:
                        try:
                            os.write(master, (command + "\n").encode())
                            self.assertEqual(process.wait(timeout=10), 0)
                        finally:
                            if process.poll() is None:
                                process.kill()
                    self.assertEqual(
                        json.loads(received.read_text()),
                        instruction_prompts.native_args(
                            provider,
                            instructions,
                            models.tiers_for(provider)["cheap"],
                            events=Crew(
                                name,
                                store.read_json(self.directory / "session.json")["crew"][name],
                                sessions.Session(self.directory, self.meta),
                            ).events,
                        ),
                    )
                finally:
                    os.close(master)
                    os.close(slave)


if __name__ == "__main__":
    unittest.main()
