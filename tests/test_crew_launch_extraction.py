"""Herdr launches stay byte-identical to the df36799 golden fixture.

Each case records a foreground crew recruit or a captain launch: every Herdr call, the
launcher script, the lifetime record (native argv with instructions and hook commands),
any generated pi extension, and the printed record. Run ids and temp paths are replaced
by placeholders. Set REGENERATE_GOLDEN=1 to rewrite the fixture.
"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, instructions, launch, lifetime, models, runtime, sessions
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane
from tests.home_isolation import HERDR, SessionCase

FIXTURE = Path(__file__).with_name("golden") / "herdr_launch_df36799.json"
TEMPLATE = Path(__file__).with_name("golden") / "init_template_df36799.toml"
REGENERATE = os.environ.get("REGENERATE_GOLDEN") == "1"
NATIVE_HOMES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GROK_HOME",
    "PI_CODING_AGENT_DIR",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
)
PI_MODELS = (("anthropic/claude-sonnet-5-5", ("claude-sonnet-5-5",)),)
CREW_MODELS = {
    "claude": "claude-sonnet-5-5",
    "codex": "gpt-6.2-sol",
    "pi": "anthropic/claude-sonnet-5-5",
    "grok": "grok-4.7",
}
NATIVE_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PROJECT_HASH = re.compile(r"\b[0-9a-f]{64}\b")
PI_CAPTAIN_EXTENSION = re.compile(r"pi-captain-[A-Za-z0-9_]+\.mjs")


class HerdrLaunchGoldenTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        for name in NATIVE_HOMES:
            os.environ.pop(name, None)
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.enterContext(patch.object(Pane, "wait_for_crew"))
        self.enterContext(patch.object(models, "pi_models", return_value=PI_MODELS))
        self.calls = []

    def herdr(self, *call, **kwargs):
        self.calls.append([list(call), kwargs])
        match call[:2]:
            case ("pane", "read"):
                return "~/project $ "
            case ("tab", "list"):
                return {"tabs": [{"tab_id": "w1:t1", "label": "Captain Barbossa", "number": 1}]}
            case ("pane", "list"):
                return {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1"}]}
            case ("tab", "create"):
                return {
                    "tab_id": "w1:t9",
                    "root_pane": {"pane_id": "w1:p9", "tab_id": "w1:t9", "terminal_id": "term-9"},
                }
        return {}

    def normalize(self, value, replacements):
        text = json.dumps(value, indent=1, sort_keys=True)
        paths = {
            str(self.root): "<root>",
            str(Path.home()): "<home>",
            str(Path.home().resolve()): "<home>",
            instructions.python_command(): "<python>",
            sys.executable: "<python>",
        }
        for old, new in sorted({**replacements, **paths}.items(), key=lambda item: -len(item[0])):
            text = text.replace(json.dumps(old)[1:-1], new)
        text = NATIVE_SESSION.sub("<native-session>", text)
        text = PROJECT_HASH.sub("<project-hash>", text)
        return json.loads(PI_CAPTAIN_EXTENSION.sub("<pi-captain-extension>", text))

    def check(self, case, observed):
        golden = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
        if REGENERATE:
            golden[case] = observed
            FIXTURE.parent.mkdir(exist_ok=True)
            FIXTURE.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
            return
        self.maxDiff = None
        self.assertEqual(observed, golden[case])

    def recruit(self, provider, stop_hooks=True):
        directory, meta = sessions.session(self.project, self.pane, create=True)
        outside = self.root / "outside" / "notes.md"
        args = cli.parser().parse_args(
            [
                "--session",
                meta["id"],
                "crew",
                "jack",
                "--agent",
                provider,
                "--model",
                CREW_MODELS[provider],
                "--task",
                "Edit src and report.",
                "--owns",
                "src/app.py",
                "--owns",
                str(outside),
                "--allow",
                "edit",
                "--placement",
                "tab",
            ]
        )
        with (
            patch.object(runtime, "herdr", side_effect=self.herdr),
            patch.object(agents, "executable", side_effect=lambda name: f"/bin/{name}"),
            patch.object(instructions, "codex_stop_hooks", return_value=stop_hooks),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            agents.create_crew(args, self.pane, self.project)
        current = sessions.Session(directory, sessions.read_session(self.project, meta["id"]).meta)
        record = current.meta["crew"]["jack"]
        launched = lifetime.read(current, record["lifetime_id"])
        argv = launched["argv"]
        extension = argv[argv.index("--extension") + 1] if "--extension" in argv else None
        observed = {
            "herdr_calls": self.calls,
            "launcher": (directory / "crew-jack.sh").read_text(encoding="utf-8"),
            "lifetime": launched,
            "extension": Path(extension).read_text(encoding="utf-8") if extension else None,
            "record": record,
            "printed": json.loads(output.getvalue()),
            "stderr": error.getvalue(),
        }
        return self.normalize(
            observed,
            {
                meta["id"]: "<session>",
                sessions.crew_prefix(meta["id"]): "<agent-prefix>",
                record["incarnation_id"]: "<incarnation>",
                record["assignment_id"]: "<assignment>",
            },
        )

    def captain(self, provider):
        args = cli.parser().parse_args(["--agent", provider, "--no-dashboard"])
        with (
            patch.object(runtime, "herdr", side_effect=self.herdr),
            patch.object(launch, "executable", side_effect=lambda name: f"/bin/{name}"),
            patch.object(launch, "check_for_update"),
            patch.object(launch, "close_panes_on_exit"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(os, "execvpe") as execute,
        ):
            launch.launch(args, self.pane, self.project)
        binary, argv, env = execute.call_args.args
        extension = argv[argv.index("--extension") + 1] if "--extension" in argv else None
        (directory,) = [path.parent for path in self.root.rglob("captain.json")]
        observed = {
            "herdr_calls": self.calls,
            "binary": binary,
            "argv": argv,
            "env": {key: value for key, value in env.items() if key.startswith("CAPTAIN_")},
            "extension": Path(extension).read_text(encoding="utf-8") if extension else None,
            "captain_json": json.loads((directory / "captain.json").read_text()),
        }
        return self.normalize(observed, {directory.name: "<session>"})

    def test_claude_crew_launch_matches_golden(self):
        self.check("crew-claude", self.recruit("claude"))

    def test_codex_crew_launch_matches_golden(self):
        self.check("crew-codex", self.recruit("codex"))

    def test_codex_crew_below_hook_floor_launch_matches_golden(self):
        self.check("crew-codex-no-stop-hooks", self.recruit("codex", stop_hooks=False))

    def test_pi_crew_launch_matches_golden(self):
        self.check("crew-pi", self.recruit("pi"))

    def test_grok_crew_launch_matches_golden(self):
        self.check("crew-grok", self.recruit("grok"))

    def test_claude_captain_launch_matches_golden(self):
        self.check("captain-claude", self.captain("claude"))

    def test_codex_captain_launch_matches_golden(self):
        self.check("captain-codex", self.captain("codex"))

    def test_pi_captain_launch_matches_golden(self):
        self.check("captain-pi", self.captain("pi"))

    def test_grok_captain_launch_matches_golden(self):
        self.check("captain-grok", self.captain("grok"))

    def test_init_writes_the_df36799_settings_template(self):
        with contextlib.chdir(self.project), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["init"]), 0)
        written = (self.project / ".captain" / "settings.toml").read_text(encoding="utf-8")
        self.assertEqual(written, TEMPLATE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
