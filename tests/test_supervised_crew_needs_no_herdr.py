"""A supervised Codex crew runs a whole assignment through app-server with Herdr unreachable."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import (
    agents,
    claude_agent,
    cli,
    codex_app_server,
    config,
    instructions,
    jsonrpc,
    lifetime,
    native_history,
    protocol,
    retention,
    runtime,
    sessions,
    store,
    supervision,
    supervisor,
)
from captain_barbossa.crew import Crew
from captain_barbossa.pane import WAKE_LINE
from captain_barbossa.runtime import CaptainError

FAKE = Path(__file__).with_name("fake_codex_app_server.py")
FAKE_CLAUDE = Path(__file__).with_name("fake_claude_host.py")
FLAGGED = "git push origin main"


class SupervisedCrewTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=0.5\nshutdown_timeout=2.0\n"
            "[supervisor]\nrequest_timeout=10.0\nstartup_timeout=15.0\nstop_timeout=10.0\n"
            "hook_timeout=20.0\n",
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
        self.fake = self.root / "fake"
        (self.fake / "control").mkdir(parents=True)
        codex = self.root / "bin" / "codex"
        codex.parent.mkdir()
        codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        codex.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"FAKE_CODEX_DIR": str(self.fake)}))
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(agents, "executable", return_value=str(codex)))
        self.enterContext(patch.object(instructions, "codex_stop_hooks", return_value=True))
        self.herdr = self.enterContext(
            patch.object(runtime, "herdr", side_effect=AssertionError("Herdr was called"))
        )
        self.current = supervisor.open_session(self.project, None)
        self.serve()

    def serve(self):
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(self.current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        self.stopped, self.thread = stopped, thread
        with os.fdopen(read_fd) as lines:
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]

    def captain(self, *argv, env=None, code=0):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env or {}),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            returned = cli.main(["--session", self.current.meta["id"], *argv])
        self.assertEqual(returned, code, errors.getvalue())
        return output.getvalue() if code == 0 else errors.getvalue()

    def recruit(self):
        printed = self.captain(
            "crew", "jack", "--agent", "codex", "--model", "gpt-6.2-sol",
            "--task", "Edit notes.md and report.", "--owns", "notes.md", "--allow", "edit",
        )  # fmt: skip
        self.until(lambda: self.inbound("turn/start"), "the assignment turn")
        return json.loads(printed.splitlines()[-1])["assignment_id"]

    def as_crew(self, assignment):
        return {
            "CAPTAIN_ROLE": "crew",
            "CAPTAIN_CREW": "jack",
            "CAPTAIN_INCARNATION": self.crew().record["incarnation_id"],
            "CAPTAIN_ASSIGNMENT": assignment,
            supervision.TOKEN_ENV: "",
        }

    def assignment(self, assignment_id):
        return store.read_json(self.current.directory / "protocol.json")["assignments"][
            assignment_id
        ]

    def log(self):
        path = self.fake / "log.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line) for line in lines]

    def inbound(self, method):
        return [e["in"] for e in self.log() if "in" in e and e["in"].get("method") == method]

    def replies(self):
        return [e["in"]["result"] for e in self.log() if "in" in e and "result" in e.get("in", {})]

    def until(self, check, what):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            found = check()
            if found:
                return found
            time.sleep(0.05)
        self.fail(
            f"timed out waiting for {what}: {supervision.state(self.crew())} "
            f"{json.dumps(self.log())[-3000:]}"
        )

    def control(self, **command):
        name = f"{time.monotonic_ns()}.json"
        staging = self.fake / name
        staging.write_text(json.dumps(command))
        staging.rename(self.fake / "control" / name)

    def crew(self):
        meta = store.read_json(self.current.meta_path)
        return Crew("jack", meta["crew"]["jack"], sessions.Session(self.current.directory, meta))

    def idle(self):
        self.until(lambda: supervision.state(self.crew()) == "idle", "an idle crew")

    def test_assign_tell_interrupt_retier_grant_done_dismiss(self):
        printed = self.captain(
            "crew", "jack", "--agent", "codex", "--model", "gpt-6.2-sol",
            "--task", "Edit notes.md and report.", "--owns", "notes.md", "--allow", "edit",
        )  # fmt: skip
        record = json.loads(printed.splitlines()[-1])
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        assignment = record["assignment_id"]
        argv = self.until(
            lambda: next((e["argv"] for e in self.log() if "argv" in e), None), "argv"
        )
        self.assertEqual(argv[0], "app-server")
        self.assertFalse([a for a in argv if a.startswith(("notify=", "--sandbox", "-m"))])
        self.assertTrue(any(a.startswith("hooks.PermissionRequest=") for a in argv))
        (thread,) = self.inbound("thread/start")
        self.assertEqual(
            thread["params"],
            {
                "cwd": str(self.project),
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "model": "gpt-6.2-sol",
            },
        )
        first = self.until(lambda: self.inbound("turn/start"), "the assignment turn")[0]
        text = first["params"]["input"][0]["text"]
        self.assertIn("Edit notes.md and report.", text)
        self.assertIn("inbox jack", text)

        # Mid-turn mail is held, then started as soon as the turn ends.
        self.captain("tell", "jack", "Also add a title.", "--assignment", assignment)
        self.assertEqual(len(self.inbound("turn/start")), 1)
        stamp = store.read_json(self.current.directory / "mail" / "jack" / ".drain")
        self.assertEqual((stamp["landed"], stamp["gate"]), (False, "agent not idle"))
        self.control(do="complete", reply="Edited.")
        turns = self.until(lambda: self.inbound("turn/start")[1:], "the held mail")
        self.assertIn("Also add a title.", turns[0]["params"]["input"][0]["text"])
        events = self.crew().events.read_text().splitlines()
        completed = [json.loads(line) for line in events if "agent-turn-complete" in line]
        self.assertEqual(completed[0]["turn-id"], "turn-1")
        self.assertEqual(completed[0]["last-assistant-message"], "Edited.")

        # Interrupt stops the live turn by id.
        self.assertIn("Interrupted Jack.", self.captain("interrupt", "jack", "--reason", "stop"))
        self.assertEqual(self.inbound("turn/interrupt")[0]["params"]["turnId"], "turn-2")
        self.idle()

        # Retier: the next turn carries the model and the roster records it.
        self.assertIn("switched to gpt-6-sol", self.captain("model", "jack", "gpt-6-sol"))
        self.assertEqual(self.crew().record["model"], "gpt-6-sol")
        self.captain("tell", "jack", "Carry on with the new model.", "--assignment", assignment)
        third = self.until(lambda: self.inbound("turn/start")[2:], "a retiered turn")[0]
        self.assertEqual(third["params"]["model"], "gpt-6-sol")

        # A flagged command is declined; one captain grant accepts it exactly once.
        self.control(do="approve", command=FLAGGED, cwd=str(self.project))
        self.until(lambda: self.replies(), "the first approval")
        self.assertEqual(self.replies()[-1], {"decision": "decline"})
        self.captain("grant", "jack", "--command", FLAGGED, "--assignment", assignment)
        self.control(do="approve", command=FLAGGED, cwd=str(self.project))
        self.until(lambda: len(self.replies()) == 2, "the granted approval")
        self.assertEqual(self.replies()[-1], {"decision": "accept"})
        state = store.read_json(self.current.directory / "protocol.json")
        self.assertEqual(state["assignments"][assignment].get("grants"), [])
        decisions = [
            event["approval"]
            for event in map(json.loads, self.crew().events.read_text().splitlines())
            if event.get("hook_event_name") == "PermissionDecision"
        ]
        self.assertEqual([d["code"] for d in decisions][-1], "captain-grant")
        self.assertEqual(decisions[0]["verdict"], "deny")
        self.control(do="ask")
        self.until(lambda: len(self.replies()) == 3, "the declined question")
        self.assertIn("captain ask", self.replies()[-1]["answers"]["q1"]["answers"][0])
        self.control(do="complete")
        self.idle()

        # The crew reports done as crew; the captain acknowledges it, then dismisses.
        crew = self.crew()
        self.captain(
            "done",
            "jack",
            "--report",
            "Edited notes.md.",
            env={
                "CAPTAIN_ROLE": "crew",
                "CAPTAIN_CREW": "jack",
                "CAPTAIN_INCARNATION": crew.record["incarnation_id"],
                "CAPTAIN_ASSIGNMENT": assignment,
                supervision.TOKEN_ENV: "",
            },
        )
        statuses, ack = [], []
        for _ in range(20):
            response = json.loads(self.captain("wait", "jack", "--json", "--timeout", "0", *ack))
            if not response["delivery_id"]:
                break
            statuses.append(response["status"])
            ack = ["--ack", response["delivery_id"]]
        self.assertEqual(statuses[-1], "done")
        self.assertIn("Dismissed Jack.", self.captain("dismiss", "jack", "--confirm-unlanded"))
        self.assertEqual(lifetime.read(self.current, crew.record["lifetime_id"])["state"], "exited")
        self.herdr.assert_not_called()

    def test_untrusted_codex_hooks_refuse_the_start(self):
        with patch.dict(os.environ, {"FAKE_CODEX_TRUST": "untrusted"}):
            errors = self.captain(
                "crew", "jack", "--agent", "codex", "--model", "gpt-6.2-sol",
                "--task", "Edit notes.md.", "--owns", "notes.md", "--allow", "edit", code=1,
            )  # fmt: skip
        self.assertIn("[codex-hooks-untrusted]", errors)
        self.assertIn("preToolUse", errors)
        self.assertEqual(self.inbound("thread/start"), [])
        self.assertEqual(self.crew().record["status"], "dismissed")
        self.herdr.assert_not_called()

    def test_grant_request_links_the_denial_to_the_ask_and_resolves_once(self):
        assignment = self.recruit()
        self.control(do="approve", command=FLAGGED, cwd=str(self.project))
        self.until(lambda: self.replies(), "the declined approval")
        self.assertEqual(self.replies()[-1], {"decision": "decline"})
        asked = json.loads(
            self.captain("ask", "jack", "I need to push to main.", env=self.as_crew(assignment))
        )
        (request,) = self.assignment(assignment)["grant_requests"]
        self.assertEqual(asked["grant_request"], request["id"])
        self.assertEqual(
            {key: request[key] for key in ("crew", "assignment", "tool", "command", "cwd")},
            {
                "crew": "jack",
                "assignment": assignment,
                "tool": "Bash",
                "command": FLAGGED,
                "cwd": str(self.project),
            },
        )
        self.assertEqual(
            (request["code"], request["category"], request["words"], request["state"]),
            ("flagged-outward", "flagged", "I need to push to main.", "open"),
        )
        self.assertEqual(request["question"], asked["question_id"])
        self.assertIn("leaves this machine", request["reason"])
        notices = self.assignment(assignment)["notices"]
        self.assertIn(f"[grant-request {request['id']}] flagged-outward", notices[-1]["summary"])

        resolved = supervision.resolve_grant(self.current.directory, request["id"], "grant")
        self.assertEqual((resolved["crew"], resolved["state"]), ("jack", "granted"))
        record = self.assignment(assignment)
        self.assertIsNone(record["question"])
        self.assertEqual([g["command"] for g in record["grants"]], [FLAGGED])
        with self.assertRaisesRegex(CaptainError, r"\[grant-request-closed\]"):
            supervision.resolve_grant(self.current.directory, request["id"], "deny", "no")
        self.control(do="approve", command=FLAGGED, cwd=str(self.project))
        self.until(lambda: len(self.replies()) == 2, "the granted approval")
        self.assertEqual(self.replies()[-1], {"decision": "accept"})
        self.assertEqual(self.assignment(assignment)["grants"], [])

        other = "git push origin release"
        self.control(do="approve", command=other, cwd=str(self.project))
        self.until(lambda: len(self.replies()) == 3, "the second declined approval")
        asked = json.loads(
            self.captain("ask", "jack", "May I push release?", env=self.as_crew(assignment))
        )
        with self.assertRaisesRegex(CaptainError, r"\[grant-request-reason\]"):
            supervision.resolve_grant(self.current.directory, asked["grant_request"], "deny")
        denied = supervision.resolve_grant(
            self.current.directory, asked["grant_request"], "deny", "release is frozen"
        )
        self.assertEqual(denied["state"], "denied")
        mail = store.read_json(
            protocol.mail_dir(self.current.directory, "jack") / f"{denied['message_id']}.json"
        )
        self.assertIn("Denied: release is frozen", mail["text"])
        self.assertEqual(self.assignment(assignment)["grants"], [])
        self.herdr.assert_not_called()

    def test_app_server_session_identity_is_deleted_on_dismiss_as_for_a_pane_crew(self):
        identity, codex_home = str(uuid.uuid4()), self.root / "codex-home"
        real_which, real_run, deleted = shutil.which, subprocess.run, []

        def which(name, *args, **kwargs):
            return "/mock/codex" if name == "codex" else real_which(name, *args, **kwargs)

        def run(argv, *args, **kwargs):
            if argv[:2] == ["/mock/codex", "delete"]:
                deleted.append((argv, kwargs["env"]["CODEX_HOME"]))
                return SimpleNamespace(returncode=0)
            return real_run(argv, *args, **kwargs)

        env = {"FAKE_CODEX_SESSION_ID": identity, "CODEX_HOME": str(codex_home)}
        with (
            patch.dict(os.environ, env),
            patch.object(native_history.shutil, "which", side_effect=which),
        ):
            self.recruit()
            self.until(
                lambda: [e for e in self.log() if e.get("hook") == "sessionStart"],
                "the SessionStart hook",
            )
        path = native_history._path(self.crew())
        self.assertEqual(store.read_json(path)["session_ids"], [identity])
        with patch.object(native_history.subprocess, "run", side_effect=run):
            self.assertIn("Dismissed Jack.", self.captain("dismiss", "jack", "--force"))
        self.assertEqual(
            deleted, [(["/mock/codex", "delete", identity, "--force"], str(codex_home))]
        )
        self.assertEqual(store.read_json(path)["state"], "complete")
        self.herdr.assert_not_called()

    def test_a_live_supervisor_keeps_its_session_from_pruning(self):
        past = time.time() - 30 * 86400
        for item in [self.current.directory, *self.current.directory.rglob("*")]:
            os.utime(item, (past, past), follow_symlinks=False)
        with patch.object(retention, "herdr", side_effect=CaptainError("Herdr unreachable")):
            self.assertEqual(retention.prune_sessions(self.project), [])
            self.assertTrue(self.current.directory.exists())
            self.stopped.set()
            self.thread.join(30)
            self.assertEqual(retention.prune_sessions(self.project), [self.current.directory])
        self.herdr.assert_not_called()

    def test_status_dashboard_and_recover_read_supervised_crew_without_herdr(self):
        self.recruit()
        status = self.captain("status")
        self.assertRegex(
            status,
            r"Jack\s+codex\s+gpt-6\.2-sol\s+supervised\s+state:working "
            r"source:supervisor",
        )
        self.assertIn("Jack", self.captain("dashboard", "--once"))
        self.assertEqual(json.loads(self.captain("recover"))["session_id"], self.current.meta["id"])
        self.herdr.assert_not_called()

    def test_interrupt_all_stops_every_working_crew_in_one_call(self):
        self.recruit()
        outcomes = supervision.interrupt_all(self.current.directory, "stop everything")
        self.assertEqual(outcomes, [{"crew": "jack", "outcome": "interrupted"}])
        self.assertEqual(self.inbound("turn/interrupt")[0]["params"]["turnId"], "turn-1")
        self.idle()
        outcomes = supervision.interrupt_all(self.current.directory)
        self.assertEqual(outcomes, [{"crew": "jack", "outcome": "idle"}])
        graph = store.read_json(self.current.graph)
        self.assertIn("stop everything", [node["label"] for node in graph["nodes"]])
        self.herdr.assert_not_called()

    def test_placement_flags_and_unsupported_providers_are_refused(self):
        for argv, pattern in (
            (("--agent", "codex", "--placement", "tab"), r"\[supervised-flags\].*--placement"),
            (("--agent", "grok"), r"\[supervised-transport\] grok"),
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(CaptainError, pattern):
                agents.create_crew(
                    cli.parser().parse_args(
                        ["--session", self.current.meta["id"], "crew", "--task", "x", *argv]
                    ),
                    None,
                    self.project,
                )
        self.assertEqual(store.read_json(self.current.meta_path)["crew"], {})
        self.herdr.assert_not_called()


class SupervisedClaudeTests(SessionCase):
    """The Codex round trip's harness, with the real claude_host over a fake Agent SDK."""

    serve, captain, as_crew, assignment, log, until, control, crew, idle = (
        SupervisedCrewTests.serve,
        SupervisedCrewTests.captain,
        SupervisedCrewTests.as_crew,
        SupervisedCrewTests.assignment,
        SupervisedCrewTests.log,
        SupervisedCrewTests.until,
        SupervisedCrewTests.control,
        SupervisedCrewTests.crew,
        SupervisedCrewTests.idle,
    )

    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[lifetime]\nstartup_timeout=5.0\npoll_interval=0.01\n"
            "terminate_timeout=2.0\nshutdown_timeout=2.0\n"
            "[supervisor]\nrequest_timeout=10.0\nstartup_timeout=15.0\nstop_timeout=10.0\n"
            "hook_timeout=20.0\n",
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
        self.fake = self.root / "fake"
        (self.fake / "control").mkdir(parents=True)
        self.claude = str(self.root / "bin" / "claude")
        home = self.root / "home"
        home.mkdir()
        self.enterContext(
            patch.dict(os.environ, {"FAKE_CLAUDE_DIR": str(self.fake), "HOME": str(home)})
        )
        os.environ.pop(supervision.TOKEN_ENV, None)
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(agents, "executable", return_value=self.claude))
        native_command = instructions.native_command
        self.enterContext(
            patch.object(
                instructions,
                "native_command",
                side_effect=lambda binary, transport="tui": (
                    [sys.executable, str(FAKE_CLAUDE), binary]
                    if transport == "sdk"
                    else native_command(binary, transport)
                ),
            )
        )
        self.herdr = self.enterContext(
            patch.object(runtime, "herdr", side_effect=AssertionError("Herdr was called"))
        )
        self.current = supervisor.open_session(self.project, None)
        self.serve()

    def recruit(self):
        printed = self.captain(
            "crew", "jack", "--agent", "claude", "--model", "claude-sonnet-5-5",
            "--task", "Edit notes.md and report.", "--owns", "notes.md", "--allow", "edit",
        )  # fmt: skip
        self.until(lambda: self.entries("query"), "the wake line")
        return json.loads(printed.splitlines()[-1])

    def entries(self, key):
        return [entry[key] for entry in self.log() if key in entry]

    def native(self, entry):
        return (entry.get("output") or {}).get("hookSpecificOutput") or {}

    def delivered(self, text):
        """The hooks whose additionalContext carried text."""
        return [
            entry["hook"]
            for entry in self.log()
            if "hook" in entry and text in self.native(entry).get("additionalContext", "")
        ]

    def pretool(self):
        return [self.native(e) for e in self.log() if e.get("hook") == "PreToolUse"]

    def events(self):
        return [json.loads(line) for line in self.crew().events.read_text().splitlines()]

    def test_assign_tell_interrupt_retier_grant_done_dismiss(self):
        record = self.recruit()
        self.assertEqual((record["execution"], record["status"]), ("supervised", "started"))
        assignment = record["assignment_id"]
        (options,) = self.entries("connect")
        self.assertEqual(options["cli_path"], self.claude)
        self.assertEqual(options["permission_mode"], "auto")
        self.assertEqual(options["setting_sources"], ["user", "project", "local"])
        self.assertIn("crew member Jack", options["system_prompt"]["append"])
        self.assertEqual(set(options["extra_args"]), {"settings", "model", "session-id"})
        self.assertFalse({"can_use_tool", "settings", "sandbox"} & set(options))
        # The doorbell is the bare wake line; claude's own hooks hand over the body.
        self.assertEqual(self.entries("query"), [WAKE_LINE])
        self.until(lambda: self.delivered("Edit notes.md and report."), "the delivered body")
        self.assertEqual(protocol.unread(self.crew()), [])

        # Mid-turn mail is held, then handed over by the Stop hook as the turn ends.
        self.captain("tell", "jack", "Also add a title.", "--assignment", assignment)
        stamp = store.read_json(self.current.directory / "mail" / "jack" / ".drain")
        self.assertEqual((stamp["landed"], stamp["gate"]), (False, "agent not idle"))
        self.control(do="complete", reply="Edited.")
        self.until(lambda: "Stop" in self.delivered("Also add a title."), "the Stop delivery")
        self.idle()

        # Interrupt ends the live turn; with no Stop hook, the supervisor records the idle.
        self.captain("tell", "jack", "Now rename it.", "--assignment", assignment)
        self.until(lambda: len(self.entries("query")) == 2, "the second wake line")
        self.assertIn("Interrupted Jack.", self.captain("interrupt", "jack", "--reason", "stop"))
        self.assertEqual(self.entries("interrupt"), [True])
        self.idle()
        self.until(
            lambda: self.events()[-1].get("notification_type") == "idle_prompt", "the idle record"
        )
        self.assertNotEqual(self.crew().current_state().state, "working")

        # Retier goes through the SDK and the roster records it.
        self.assertIn("applies now", self.captain("model", "jack", "claude-opus-5-5"))
        self.assertEqual(self.entries("set_model"), ["claude-opus-5-5"])
        self.assertEqual(self.crew().record["model"], "claude-opus-5-5")

        # claude's PreToolUse hook denies a flagged command; one captain grant allows it once.
        self.control(do="tool", command=FLAGGED)
        self.until(self.pretool, "the first PreToolUse verdict")
        self.assertEqual(self.pretool()[-1]["permissionDecision"], "deny")
        self.captain("grant", "jack", "--command", FLAGGED, "--assignment", assignment)
        self.control(do="tool", command=FLAGGED)
        self.until(lambda: len(self.pretool()) == 2, "the granted PreToolUse verdict")
        self.assertEqual(self.pretool()[-1]["permissionDecision"], "allow")
        self.assertEqual(self.assignment(assignment).get("grants"), [])

        crew = self.crew()
        self.captain("done", "jack", "--report", "Edited notes.md.", env=self.as_crew(assignment))
        statuses, ack = [], []
        for _ in range(20):
            response = json.loads(self.captain("wait", "jack", "--json", "--timeout", "0", *ack))
            if not response["delivery_id"]:
                break
            statuses.append(response["status"])
            ack = ["--ack", response["delivery_id"]]
        self.assertEqual(statuses[-1], "done")
        self.assertIn("Dismissed Jack.", self.captain("dismiss", "jack", "--confirm-unlanded"))
        launch = lifetime.read(self.current, crew.record["lifetime_id"])
        # The host closed claude itself, so claude's 143 counts as a clean exit.
        self.assertEqual((launch["state"], launch["native_returncode"]), ("exited", 0))
        self.assertEqual(self.entries("disconnect"), [True])
        self.herdr.assert_not_called()

    def test_retier_keeps_the_user_claude_settings(self):
        self.recruit()
        settings = Path.home() / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"model": "sonnet"}\n')
        self.assertIn("switched to claude-opus-5-5", self.captain("model", "jack", "opus"))
        self.assertEqual(self.entries("set_model"), ["claude-opus-5-5"])
        self.assertEqual(settings.read_text(), '{"model": "sonnet"}\n')
        self.assertEqual([path.name for path in settings.parent.iterdir()], ["settings.json"])
        self.herdr.assert_not_called()

    def test_a_denied_call_becomes_a_grant_request_the_app_resolves(self):
        assignment = self.recruit()["assignment_id"]
        self.control(do="tool", command=FLAGGED)
        self.until(self.pretool, "the denial")
        asked = json.loads(
            self.captain("ask", "jack", "I need to push to main.", env=self.as_crew(assignment))
        )
        (request,) = self.assignment(assignment)["grant_requests"]
        self.assertEqual(asked["grant_request"], request["id"])
        self.assertEqual(
            (request["tool"], request["command"], request["code"]),
            ("Bash", FLAGGED, "flagged-outward"),
        )
        resolved = supervision.resolve_grant(self.current.directory, request["id"], "grant")
        self.assertEqual(resolved["state"], "granted")
        self.control(do="tool", command=FLAGGED)
        self.until(lambda: len(self.pretool()) == 2, "the granted call")
        self.assertEqual(self.pretool()[-1]["permissionDecision"], "allow")
        self.herdr.assert_not_called()

    def test_interrupt_all_stops_a_working_claude_crew(self):
        self.recruit()
        outcomes = supervision.interrupt_all(self.current.directory, "stop everything")
        self.assertEqual(outcomes, [{"crew": "jack", "outcome": "interrupted"}])
        self.idle()
        outcomes = supervision.interrupt_all(self.current.directory)
        self.assertEqual(outcomes, [{"crew": "jack", "outcome": "idle"}])
        self.herdr.assert_not_called()

    def test_an_auth_failure_posts_one_claude_auth_notice(self):
        assignment = self.recruit()["assignment_id"]
        self.control(do="auth")

        def notices():
            return [
                notice["summary"]
                for notice in self.assignment(assignment)["notices"]
                if "[claude-auth]" in notice["summary"]
            ]

        self.until(notices, "the claude-auth notice")
        self.assertIn("claude auth login", notices()[0])
        self.herdr.assert_not_called()

    def test_a_missing_sdk_refuses_the_start_with_the_install_hint(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_SDK": "missing"}):
            errors = self.captain(
                "crew", "jack", "--agent", "claude", "--model", "claude-sonnet-5-5",
                "--task", "Edit notes.md.", "--owns", "notes.md", "--allow", "edit", code=1,
            )  # fmt: skip
        self.assertIn("[claude-sdk-missing]", errors)
        self.assertEqual(self.entries("connect"), [])
        self.assertEqual(self.crew().record["status"], "dismissed")
        self.herdr.assert_not_called()

    def test_a_failed_connect_closes_the_peers_write_end(self):
        peers = []

        class Recording(jsonrpc.Peer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                peers.append(self)

        with (
            patch.object(jsonrpc, "Peer", Recording),
            patch.dict(os.environ, {"FAKE_CLAUDE_SDK": "missing"}),
        ):
            self.captain(
                "crew", "jack", "--agent", "claude", "--model", "claude-sonnet-5-5",
                "--task", "Edit notes.md.", "--owns", "notes.md", "--allow", "edit", code=1,
            )  # fmt: skip
        (peer,) = peers
        self.until(lambda: peer.write_fd is None, "the peer's write end closed")
        self.herdr.assert_not_called()


class FakePeer:
    def __init__(self, error=None):
        self.requests, self.error = [], error

    def request(self, method, params, timeout):
        self.requests.append(method)
        if self.error:
            raise CaptainError(self.error)
        return {"turn": {"id": f"turn-{len(self.requests)}"}}


class SupervisedDeliveryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, None, create=True)
        record = {
            "id": "jack",
            "name": "Jack",
            "agent": "c-jack",
            "incarnation_id": "a" * 32,
            "provider": "codex",
            "execution": "supervised",
        }
        self.crew = Crew("jack", record, self.current)
        protocol.mail_dir(self.current.directory, "jack").mkdir(parents=True)

    def link(self, peer):
        link = supervisor.Link("jack", "a" * 32, codex_app_server, self.crew.events)
        link.peer, link.thread, link.ended = peer, "thread-1", lambda event: None
        return link

    def test_racing_rings_keep_the_landing(self):
        calls, second = [], threading.Event()

        def ring(crew, message_id, text):
            calls.append(message_id)
            if len(calls) == 1:
                second.wait(0.5)  # a second ring that got in now would land first
                return {"landed": False, "gate": "agent not idle"}
            second.set()
            return {"landed": True, "gate": None}

        with patch.object(supervision, "ring", side_effect=ring):
            first = threading.Thread(target=protocol.ring, args=(self.crew, "m-1"))
            first.start()
            deadline = time.monotonic() + 5
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            protocol.ring(self.crew, "m-1")
            first.join(5)
        self.assertIn("m-1", protocol.landed_bodies(self.crew))

    def test_an_accepted_message_never_starts_a_second_turn(self):
        peer = FakePeer()
        link = self.link(peer)
        self.assertTrue(codex_app_server.ring(link, "m-1", "body")["landed"])
        turn = {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
        codex_app_server.handle(link, turn)
        self.assertEqual(codex_app_server.ring(link, "m-1", "body"), {"landed": True, "gate": None})
        self.assertEqual(peer.requests, ["turn/start"])

    def test_an_unanswered_turn_start_is_neither_bounced_nor_resent(self):
        peer = FakePeer("[jsonrpc-timeout] No answer to turn/start within 10s.")
        link = self.link(peer)
        result = codex_app_server.ring(link, "m-1", "body")
        self.assertEqual(result, {"landed": False, "gate": codex_app_server.UNCONFIRMED})
        self.assertEqual(codex_app_server.ring(link, "m-2", "other")["gate"], "agent not idle")
        started = {"method": "turn/started", "params": {"turn": {"id": "turn-9"}}}
        codex_app_server.handle(link, started)
        self.assertEqual(link.turn, "turn-9")
        self.assertTrue(codex_app_server.ring(link, "m-1", "body")["landed"])
        self.assertEqual(peer.requests, ["turn/start"])
        with (
            patch.object(supervision, "ring", return_value=result),
            patch.object(protocol, "bounce") as bounce,
        ):
            protocol.ring(self.crew, "m-1")
        bounce.assert_not_called()


class SupervisedClaudeDeliveryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.current = sessions.session(self.project, None, create=True)
        record = {
            "id": "jack",
            "name": "Jack",
            "agent": "c-jack",
            "incarnation_id": "a" * 32,
            "provider": "claude",
            "execution": "supervised",
        }
        self.crew = Crew("jack", record, self.current)
        self.recorded, self.rung = [], []

    def link(self, peer):
        link = supervisor.Link("jack", "a" * 32, claude_agent, self.crew.events)
        link.peer, link.thread, link.root = peer, "session-1", str(self.project)
        link.relay = self.recorded.append
        link.ended = self.rung.append
        return link

    def test_an_unanswered_query_keeps_the_slot_busy_and_is_never_resent(self):
        peer = FakePeer("[jsonrpc-timeout] No answer to query within 10s.")
        link = self.link(peer)
        result = claude_agent.ring(link, "m-1", "body")
        self.assertEqual(result, {"landed": False, "gate": claude_agent.UNCONFIRMED})
        self.assertEqual(claude_agent.state(link), "working")
        self.assertEqual(claude_agent.ring(link, "m-2", "other")["gate"], "agent not idle")
        self.assertEqual(claude_agent.ring(link, "m-1", "body")["gate"], "agent not idle")
        claude_agent.handle(link, {"method": "turn", "params": {"state": "working"}})
        self.assertEqual(claude_agent.ring(link, "m-1", "body"), {"landed": True, "gate": None})
        self.assertEqual(peer.requests, ["query"])

    def test_a_refused_query_frees_the_slot(self):
        link = self.link(FakePeer("[jsonrpc-error] query: no"))
        with self.assertRaises(CaptainError):
            claude_agent.ring(link, "m-1", "body")
        self.assertEqual(claude_agent.state(link), "idle")

    def test_a_ring_waits_for_the_interrupted_turns_idle_record(self):
        peer = FakePeer()
        link = self.link(peer)
        release, relaying = threading.Event(), threading.Event()

        def relay(event):
            relaying.set()
            release.wait(5)
            self.recorded.append(event)

        link.relay = relay
        self.assertTrue(claude_agent.ring(link, "m-1", "body")["landed"])
        claude_agent.handle(link, {"method": "turn", "params": {"state": "idle", "clean": False}})
        self.assertTrue(relaying.wait(5))
        self.assertEqual(claude_agent.ring(link, "m-2", "next")["gate"], "agent not idle")
        self.assertEqual(peer.requests, ["query"])
        release.set()
        deadline = time.monotonic() + 5
        while not self.rung and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.rung, [None])
        self.assertEqual(self.recorded[0]["notification_type"], "idle_prompt")
        self.assertTrue(claude_agent.ring(link, "m-2", "next")["landed"])
        self.assertEqual(peer.requests, ["query", "query"])


if __name__ == "__main__":
    unittest.main()
