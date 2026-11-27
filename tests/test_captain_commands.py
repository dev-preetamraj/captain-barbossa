"""Captain commands and skills: one listing shape for the running captain and the session-less
catalogue, Claude's skills told apart by their SKILL.md, and listed commands run on request."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    claude_agent,
    claude_host,
    cli,
    codex_app_server,
    command_catalogue,
    config,
    eventlog,
    pi_rpc,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.runtime import CaptainError

NATIVE = [sys.executable, "-c", "import sys; sys.stdin.read()"]
SERVER = [
    {
        "name": "compact",
        "description": "Free up context",
        "argumentHint": "<notes>",
        "builtin": True,
    },
    {"name": "alpha", "description": "Alpha skill (user)", "argumentHint": ""},
    {"name": "hidden", "description": "Model only (user)", "argumentHint": ""},
    {"name": "manual", "description": "Manual skill (user)", "argumentHint": ""},
    {"name": "deploy", "description": "Deploy it (project)", "argumentHint": "[env]"},
    {"name": "pl:beta", "description": "(pl) Beta", "argumentHint": ""},
    {"name": "folded", "description": "Folded skill (user)", "argumentHint": ""},
]


def skill(directory, name, *front):
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(("---", *front, "---", "Body.")), encoding="utf-8")


class ClaudeListingTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ))
        home = self.root / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(home)
        skill(home / "skills", "alpha", "name: alpha", "description: Alpha does a thing.")
        skill(home / "skills", "hidden", "name: hidden", "user-invocable: false")
        skill(home / "skills", "manual", "disable-model-invocation: true")
        skill(home / "skills", "folded", "description: >", "  Folded text.")
        commands = self.project / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "deploy.md").write_text("Deploy $ARGUMENTS", encoding="utf-8")
        plugin = self.root / "plugin"
        skill(plugin / "skills", "beta", "description: Beta from its plugin.")
        installed = {"version": 2, "plugins": {"pl@market": [{"installPath": str(plugin)}]}}
        (home / "plugins").mkdir(parents=True)
        (home / "plugins" / "installed_plugins.json").write_text(json.dumps(installed), "utf-8")

    def test_claude_skills_come_from_skill_files_and_hidden_ones_are_left_out(self):
        listing = command_catalogue.claude_listing(SERVER, self.project)
        self.assertEqual(
            listing["commands"],
            [
                {"name": "compact", "description": "Free up context", "argument_hint": "<notes>"},
                {"name": "deploy", "description": "Deploy it (project)", "argument_hint": "[env]"},
            ],
        )
        self.assertEqual(
            listing["skills"],
            [
                {"name": "alpha", "description": "Alpha does a thing."},
                {"name": "manual", "description": "Manual skill (user)"},
                {"name": "pl:beta", "description": "Beta from its plugin."},
                {"name": "folded", "description": "Folded skill (user)"},
            ],
        )

    def test_the_running_claude_captain_lists_from_its_initialize_reply(self):
        link = types.SimpleNamespace(native={"commands": SERVER}, root=self.project)
        self.assertEqual(
            claude_agent.commands(link), command_catalogue.claude_listing(SERVER, self.project)
        )

    def test_session_less_claude_listing_reads_the_host_reply(self):
        done = subprocess.CompletedProcess([], 0, json.dumps({"commands": SERVER[:2]}) + "\n", "")
        with (
            patch.object(command_catalogue.processes, "capture", return_value=done) as capture,
            patch.object(command_catalogue.runtime, "executable", return_value="claude"),
        ):
            listing = command_catalogue.catalogue(self.project, "claude")
        self.assertEqual([row["name"] for row in listing["commands"]], ["compact"])
        self.assertEqual(
            listing["skills"], [{"name": "alpha", "description": "Alpha does a thing."}]
        )
        self.assertEqual(capture.call_args.args[1][-2:], ["--commands", "claude"])
        failed = subprocess.CompletedProcess(
            [], 1, json.dumps({"error": "[claude-sdk-missing] x"}), ""
        )
        with (
            patch.object(command_catalogue.processes, "capture", return_value=failed),
            patch.object(command_catalogue.runtime, "executable", return_value="claude"),
            self.assertRaisesRegex(CaptainError, r"\[claude-sdk-missing\]"),
        ):
            command_catalogue.catalogue(self.project, "claude")
        with self.assertRaisesRegex(CaptainError, "supports claude, codex, pi"):
            command_catalogue.catalogue(self.project, "grok")


class CommandsCliTests(SessionCase):
    def test_captain_commands_lists_one_agent_as_json_without_a_session(self):
        listing = {"commands": [{"name": "compact"}], "skills": [{"name": "ship"}]}
        found = {}

        def discover(root):
            found["root"] = root
            return listing

        with (
            patch.dict(command_catalogue.DISCOVER, {"codex": discover}),
            patch.object(cli, "current_pane", side_effect=AssertionError("Herdr called")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["commands", "--agent", "codex", "--cwd", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), command_catalogue.normalize(listing))
        self.assertEqual(found["root"], self.root)

    def test_a_failed_listing_is_a_json_error(self):
        with (
            patch.dict(
                command_catalogue.DISCOVER,
                {"pi": lambda root: (_ for _ in ()).throw(CaptainError("pi did not answer"))},
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["commands", "--agent", "pi", "--cwd", str(self.root), "--json"])
        self.assertEqual(code, 1)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(error["code"], "command-discovery")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["commands", "--agent", "pi", "--cwd", str(self.root / "no"), "--json"])
        self.assertEqual(
            (code, json.loads(output.getvalue())["error"]["code"]), (1, "commands-cwd")
        )


class ClaudeHostCommandsTests(SessionCase):
    def test_the_host_writes_the_server_commands_and_disconnects_without_a_turn(self):
        calls = []

        class Client:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                calls.append("connect")

            async def get_server_info(self):
                return {"commands": SERVER[:1]}

            async def query(self, prompt):
                calls.append("query")

            async def disconnect(self):
                calls.append("disconnect")

        fake = types.SimpleNamespace(
            ClaudeAgentOptions=lambda **given: given,
            ClaudeSDKClient=Client,
            ClaudeSDKError=Exception,
        )
        read_fd, write_fd = os.pipe()
        with patch.dict(sys.modules, {"claude_agent_sdk": fake}):
            code = asyncio.run(claude_host.list_commands(["claude"], write_fd))
        os.close(write_fd)
        with os.fdopen(read_fd) as reply:
            self.assertEqual(json.loads(reply.read()), {"commands": SERVER[:1]})
        self.assertEqual((code, calls), (0, ["connect", "disconnect"]))


class ListingShapeTests(SessionCase):
    def test_every_provider_listing_has_one_shape(self):
        codex = command_catalogue.normalize(
            codex_app_server.listing(
                {"data": [{"skills": [{"name": "s", "enabled": True}, {"name": "off"}]}]}
            )
        )
        pi = command_catalogue.normalize(
            pi_rpc.listing(
                {
                    "commands": [
                        {"name": "fix", "source": "prompt"},
                        {"name": "skill:s", "source": "skill"},
                    ]
                }
            )
        )
        self.assertEqual(
            [(row["name"], row["argument_hint"]) for row in codex["commands"]],
            [
                ("compact", None),
                ("review", "[instructions]"),
                ("goal", "<objective> | clear"),
                ("feedback", "[note]"),
            ],
        )
        self.assertEqual(codex["skills"], [{"name": "s", "description": None}])
        self.assertEqual(
            pi,
            {
                "commands": [
                    {
                        "name": "compact",
                        "description": "Summarize the session to free context.",
                        "argument_hint": "[instructions]",
                    },
                    {"name": "fix", "description": None, "argument_hint": None},
                ],
                "skills": [{"name": "skill:s", "description": None}],
            },
        )
        loose = {"commands": [{"name": "x", "description": "d"}, {"bad": 1}], "skills": None}
        self.assertEqual(
            command_catalogue.normalize(loose),
            {"commands": [{"name": "x", "description": "d", "argument_hint": None}], "skills": []},
        )

    def test_a_claude_command_runs_as_its_slash_prompt(self):
        requests = []
        peer = types.SimpleNamespace(
            request=lambda method, params, timeout: requests.append((method, params)) or {}
        )
        link = types.SimpleNamespace(
            guard=threading.Lock(), native={}, turn=None, captain=True, peer=peer
        )
        self.assertTrue(claude_agent.run_command(link, "compact", "keep the plan")["landed"])
        self.assertEqual(
            requests, [("query", {"text": "/compact keep the plan", "attachments": []})]
        )


class Recorder:
    def __init__(self, answers=None):
        self.answers, self.sent = answers or {}, []

    def request(self, method, params, timeout):
        self.sent.append((method, params))
        answer = self.answers.get(method, {})
        return answer() if callable(answer) else answer


def adapter_link(peer, **fields):
    values = dict(
        guard=threading.Lock(),
        native={},
        turn=None,
        thread="t1",
        root="/work",
        model=None,
        writable=(),
        captain=True,
        peer=peer,
    )
    return types.SimpleNamespace(**{**values, **fields})


class CodexCommandTests(SessionCase):
    SKILLS = {"data": [{"skills": [{"name": "ship", "path": "/s/SKILL.md", "enabled": True}]}]}

    def test_builtins_run_as_app_server_methods_and_hold_the_turn_slot(self):
        peer = Recorder({"skills/list": self.SKILLS})
        link = adapter_link(peer)
        self.assertTrue(codex_app_server.run_command(link, "compact", "")["landed"])
        self.assertEqual(link.turn, codex_app_server.STARTING)
        busy = codex_app_server.run_command(link, "review", "")
        self.assertEqual(busy, {"landed": False, "gate": "agent not idle"})
        link.turn = None
        codex_app_server.run_command(link, "review", "look at auth")
        link.turn = None
        codex_app_server.run_command(link, "goal", "ship the release")
        codex_app_server.run_command(link, "goal", "clear")
        codex_app_server.run_command(link, "feedback", "slow turns")
        with self.assertRaisesRegex(CaptainError, r"\[captain-command-args\]"):
            codex_app_server.run_command(link, "goal", " ")
        self.assertEqual(
            peer.sent,
            [
                ("thread/compact/start", {"threadId": "t1"}),
                (
                    "review/start",
                    {
                        "threadId": "t1",
                        "target": {"type": "custom", "instructions": "look at auth"},
                    },
                ),
                (
                    "thread/goal/set",
                    {
                        "threadId": "t1",
                        "objective": "ship the release",
                        "origin": "user",
                        "status": "active",
                    },
                ),
                ("thread/goal/clear", {"threadId": "t1", "origin": "user"}),
                (
                    "feedback/upload",
                    {
                        "threadId": "t1",
                        "classification": "other",
                        "reason": "slow turns",
                        "includeLogs": False,
                    },
                ),
            ],
        )

    def test_a_skill_runs_as_an_inline_mention_with_its_skill_item(self):
        peer = Recorder({"skills/list": self.SKILLS, "turn/start": {"turn": {"id": "u1"}}})
        link = adapter_link(peer)
        self.assertTrue(codex_app_server.run_command(link, "ship", "now")["landed"])
        method, params = peer.sent[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(
            params["input"],
            [
                {"type": "text", "text": "$ship now"},
                {"type": "skill", "name": "ship", "path": "/s/SKILL.md"},
            ],
        )
        with self.assertRaisesRegex(CaptainError, r"\[captain-command-unsupported\]"):
            codex_app_server.run_command(link, "gone", "")


class PiCommandTests(SessionCase):
    def test_compact_runs_in_the_background_and_ends_the_turn(self):
        release, ended = threading.Event(), threading.Event()
        peer = Recorder(
            {"get_state": {"isStreaming": False}, "compact": lambda: release.wait(10) and {}}
        )
        link = adapter_link(peer, notice=lambda key, text: None, ended=lambda event: ended.set())
        self.assertTrue(pi_rpc.run_command(link, "compact", "keep the plan")["landed"])
        self.assertEqual(link.turn, pi_rpc.COMPACTING)
        busy = pi_rpc.run_command(link, "compact", "")
        self.assertEqual(busy, {"landed": False, "gate": "agent not idle"})
        release.set()
        self.assertTrue(ended.wait(10))
        self.assertIsNone(link.turn)
        self.assertIn(("compact", {"customInstructions": "keep the plan"}), peer.sent)

    def test_other_commands_and_skills_go_as_the_slash_prompt(self):
        peer = Recorder({"get_state": {"isStreaming": False}, "prompt": {"disposition": "started"}})
        link = adapter_link(peer)
        self.assertTrue(pi_rpc.run_command(link, "skill:ship", "now")["landed"])
        self.assertEqual(peer.sent[-1], ("prompt", {"message": "/skill:ship now"}))


class SupervisorCaptainCommandTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=2.0\n",
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.ran, self.busy = [], threading.Event()
        self.listing = {
            "commands": [{"name": "compact", "description": "Compact."}],
            "skills": [{"name": "review", "description": "Review."}],
        }

        def run_command(link, name, args):
            if self.busy.is_set():
                return {"landed": False, "gate": "agent not idle"}
            self.ran.append((name, args))
            return {"landed": True, "gate": None}

        adapter = types.SimpleNamespace(
            open=lambda link: setattr(link, "thread", "thread-1"),
            state=lambda link: "idle",
            ring=lambda link, message_id, text: {"landed": True, "gate": None},
            interrupt=lambda link, reason: "idle",
            commands=lambda link: self.listing,
            run_command=run_command,
        )
        self.enterContext(patch.dict(supervisor.ADAPTERS, {"fake": adapter}))
        self.enterContext(
            patch.object(
                supervisor,
                "captain_command",
                side_effect=lambda sup, provider, model, effort: (
                    NATIVE,
                    dict(os.environ),
                    model,
                    "rpc",
                ),
            )
        )
        self.current = supervisor.open_session(self.project, None)
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            ready = json.loads(lines.readline())
        os.environ[supervision.TOKEN_ENV] = ready["token"]

    def call(self, method, params=None):
        return supervision.request(self.current.directory, method, params or {}, timeout=30)

    def test_listed_commands_and_skills_run_and_are_recorded(self):
        self.call("start_captain", {"provider": "fake"})
        listing = self.call("captain_commands")
        self.assertEqual(listing, command_catalogue.normalize(self.listing))
        reply = self.call("run_captain_command", {"name": "/compact", "args": "keep it"})
        self.assertEqual(reply, {"name": "compact", "args": "keep it", "state": "started"})
        self.call("run_captain_command", {"name": "review"})
        self.assertEqual(self.ran, [("compact", "keep it"), ("review", "")])
        eventlog.flush_actions(self.current.directory)
        rows, _ = eventlog.read_events(self.current.directory / eventlog.ACTIONS, 0)
        self.assertEqual(
            [
                (row["crew"], row["name"], row["args"], row["state"])
                for row in rows
                if row["kind"] == "captain-command"
            ],
            [("captain", "compact", "keep it", "started"), ("captain", "review", "", "started")],
        )

    def test_an_unlisted_command_or_a_busy_captain_is_refused(self):
        self.call("start_captain", {"provider": "fake"})
        with self.assertRaisesRegex(CaptainError, r"\[captain-command-unsupported\]"):
            self.call("run_captain_command", {"name": "goal"})
        with self.assertRaisesRegex(CaptainError, r"\[supervisor-params\]"):
            self.call("run_captain_command", {"name": "compact", "args": 3})
        self.busy.set()
        with self.assertRaisesRegex(CaptainError, r"\[captain-command-busy\]"):
            self.call("run_captain_command", {"name": "compact"})
        self.assertEqual(self.ran, [])

    def test_a_changed_listing_is_sent_to_subscribers(self):
        sent = []
        run = types.SimpleNamespace(
            link=types.SimpleNamespace(
                adapter=types.SimpleNamespace(commands=lambda link: self.listing)
            ),
            commands=None,
        )
        with patch.object(
            supervisor,
            "publish",
            side_effect=lambda owner, method, params: sent.append((method, params)),
        ):
            supervisor.refresh_commands(None, run)
            supervisor.refresh_commands(None, run)
            self.listing = {"commands": [], "skills": [{"name": "new"}]}
            supervisor.refresh_commands(None, run)
        self.assertEqual(
            sent,
            [
                (
                    "captain_commands",
                    {"commands": [], "skills": [{"name": "new", "description": None}]},
                )
            ],
        )
        self.assertIn("captain_commands", supervisor.NOTIFICATIONS)
