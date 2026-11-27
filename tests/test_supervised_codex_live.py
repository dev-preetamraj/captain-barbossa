"""Opt-in real Codex smoke tests; retain the user's authentication and hook trust."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("CAPTAIN_LIVE_CODEX") == "1", "real Codex is opt-in")
class LiveCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="captain-live-"))
        ).resolve()
        self.project = Path(self.scratch) / "project"
        self.project.mkdir()
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("CAPTAIN_")
        }
        self.env.update(
            CAPTAIN_MEMORY_ROOT=str(Path(self.scratch) / "state"),
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.project)], check=True, env=self.env
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "live@example.test"],
            check=True,
            env=self.env,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "Live Smoke"],
            check=True,
            env=self.env,
        )
        (self.project / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.project), "add", "README.md"], check=True, env=self.env
        )
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-qm", "seed"], check=True, env=self.env
        )
        remote = self.scratch / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.env)
        subprocess.run(
            ["git", "-C", str(self.project), "remote", "add", "origin", str(remote)],
            check=True,
            env=self.env,
        )
        self.command = [sys.executable, "-m", "captain_barbossa"]
        self.errors = self.enterContext(tempfile.TemporaryFile(mode="w+"))
        self.server = subprocess.Popen(
            [*self.command, "supervise"],
            cwd=self.project,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self.errors,
            text=True,
        )
        self.addCleanup(self.stop)
        self.assertTrue(select.select([self.server.stdout], [], [], 40)[0], "readiness timed out")
        line = self.server.stdout.readline()
        if not line:
            self.errors.seek(0)
            self.fail(self.errors.read())
        self.ready = json.loads(line)
        self.env["CAPTAIN_SUPERVISOR_TOKEN"] = self.ready["token"]
        self.session = next(
            path
            for path in (Path(self.env["CAPTAIN_MEMORY_ROOT"])).rglob(self.ready["session_id"])
            if path.is_dir()
        )

    def request(self, method: str, **params: object) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(40)
            connection.connect(self.ready["socket"])
            message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": {"token": self.ready["token"], **params},
            }
            connection.sendall(json.dumps(message).encode() + b"\n")
            with connection.makefile("rb") as response:
                reply = json.loads(response.readline())
            self.assertNotIn("error", reply, reply)
            return reply["result"]

    def stop(self) -> None:
        try:
            if self.server.poll() is None:
                if hasattr(self, "ready"):
                    self.request("shutdown")
                else:
                    self.server.terminate()
                self.server.wait(timeout=60)
        finally:
            if self.server.poll() is None:
                self.server.kill()
                self.server.wait(timeout=10)
            self.server.stdout.close()

    def captain(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, "--session", self.ready["session_id"], *args],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
        )

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def until(self, check, what: str, timeout: float = 180) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if found := check():
                return found
            time.sleep(0.1)
        self.errors.seek(0)
        diagnostics = [self.errors.read()]
        for path in sorted(self.session.rglob("*")):
            if path.is_file() and path.stat().st_size < 1024 * 1024:
                try:
                    diagnostics.append(
                        f"\n--- {path.relative_to(self.session)} ---\n{path.read_text()}"
                    )
                except UnicodeDecodeError:
                    pass
        self.fail(f"timed out waiting for {what}{''.join(diagnostics)}")

    def assert_codex_approval_shapes(self) -> None:
        schemas = self.scratch / "schemas"
        generated = subprocess.run(
            ["codex", "app-server", "generate-json-schema", "--out", str(schemas)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        command = self.read_json(schemas / "CommandExecutionRequestApprovalParams.json")
        self.assertEqual(command["properties"]["command"]["type"], ["string", "null"])
        self.assertTrue({"itemId", "turnId", "cwd"} <= command["properties"].keys())
        file_change = self.read_json(schemas / "FileChangeRequestApprovalParams.json")
        self.assertNotIn("command", file_change["properties"])
        self.assertTrue({"itemId", "turnId"} <= file_change["properties"].keys())
        item_started = self.read_json(schemas / "v2" / "ItemStartedNotification.json")
        change = item_started["definitions"]["FileUpdateChange"]
        self.assertEqual(set(change["required"]), {"diff", "kind", "path"})
        kinds = item_started["definitions"]["PatchChangeKind"]["oneOf"]
        self.assertEqual(
            {row["properties"]["type"]["enum"][0] for row in kinds},
            {"add", "delete", "update"},
        )
        update = next(row for row in kinds if row["properties"]["type"]["enum"] == ["update"])
        self.assertIn("move_path", update["properties"])

    def test_real_codex_supervised_lifecycle(self) -> None:
        self.assert_codex_approval_shapes()
        started = self.captain(
            "crew",
            "Jack",
            "--agent",
            "codex",
            "--model",
            "gpt-5.6-sol",
            "--task",
            "Run `sleep 30`, then follow the captain's next instructions for notes.md and Git.",
            "--owns",
            "notes.md",
            "--allow",
            "edit",
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        record = json.loads(started.stdout.splitlines()[-1])
        self.assertEqual(record["status"], "started")
        assignment_id = record["assignment_id"]
        incarnation = record["incarnation_id"]
        lifetime_id = record["lifetime_id"]

        follow_up = (
            "Use apply_patch, not a shell command, to add notes.md containing only "
            "`live smoke`. Then run exactly `git push origin main`. If it is denied, "
            "immediately run captain ask Jack 'May I run git push origin main?' and stop. "
            "After it is granted, rerun exactly the same command. Regardless of Git's result, "
            "run captain done Jack --report 'live smoke complete'."
        )
        told = self.captain("tell", "Jack", follow_up, "--assignment", assignment_id)
        self.assertEqual(told.returncode, 0, told.stdout + told.stderr)
        switched = self.captain("model", "Jack", "gpt-6-sol")
        self.assertEqual(switched.returncode, 0, switched.stdout + switched.stderr)
        self.assertIn("applies from its next turn", switched.stdout)
        interrupted = self.captain("interrupt", "Jack", "--reason", "live smoke")
        self.assertEqual(interrupted.returncode, 0, interrupted.stdout + interrupted.stderr)
        self.assertIn("Interrupted Jack", interrupted.stdout)

        protocol_path = self.session / "protocol.json"

        def open_request() -> dict | None:
            assignment = self.read_json(protocol_path)["assignments"][assignment_id]
            return next(
                (
                    request
                    for request in assignment.get("grant_requests", [])
                    if request["state"] == "open"
                ),
                None,
            )

        request = self.until(open_request, "the declined command to reach captain ask")
        self.assertEqual(request["command"], "git push origin main")
        self.assertEqual((request["tool"], request["code"]), ("Bash", "flagged-outward"))
        granted = self.request("resolve_grant", request=request["id"], decision="grant")
        self.assertEqual(granted["state"], "granted")

        def done() -> dict | None:
            assignment = self.read_json(protocol_path)["assignments"][assignment_id]
            return assignment if assignment["state"] == "done" else None

        self.until(done, "the crew's done report")
        self.until(
            lambda: self.request("state", crew="jack")["state"] == "idle",
            "the final turn to complete",
        )
        events_path = self.session / "events" / f"jack-{incarnation}.jsonl"

        def completed_turns() -> list[dict] | None:
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            found = [event for event in events if event.get("type") == "agent-turn-complete"]
            return found if len(found) >= 2 else None

        turns = self.until(completed_turns, "the completed turn events")
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        self.assertTrue(any(event.get("last-assistant-message") for event in turns))
        approvals = [
            event for event in events if event.get("hook_event_name") == "PermissionDecision"
        ]
        command_approvals = [
            event
            for event in approvals
            if event.get("tool_name") == "Bash"
            and event.get("tool_input", {}).get("command") == "git push origin main"
        ]
        self.assertIn("deny", [event["approval"]["verdict"] for event in command_approvals])
        self.assertIn("captain-grant", [event["approval"]["code"] for event in command_approvals])
        patch_approvals = [
            event
            for event in approvals
            if event.get("tool_name") == "apply_patch"
            and "notes.md" in event.get("tool_input", {}).get("command", "")
        ]
        self.assertTrue(patch_approvals)
        self.assertEqual((self.project / "notes.md").read_text().strip(), "live smoke")

        acknowledged = None
        for _ in range(20):
            args = ["wait", "Jack", "--json", "--timeout", "0"]
            if acknowledged:
                args += ["--ack", acknowledged]
            waited = self.captain(*args)
            self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
            notice = json.loads(waited.stdout)
            acknowledged = notice.get("delivery_id")
            if not acknowledged:
                break
        self.assertIsNone(acknowledged)

        manifest_path = self.session / "native-history" / f"jack-{incarnation}.json"
        manifest = self.read_json(manifest_path)
        self.assertEqual(len(manifest["session_ids"]), 1)
        session_id = manifest["session_ids"][0]
        session_starts = [
            event for event in events if event.get("hook_event_name") == "SessionStart"
        ]
        self.assertEqual(session_starts[-1]["session_id"], session_id)
        self.assertEqual(session_starts[-1]["model"], "gpt-6-sol")
        launch_path = self.session / "lifetimes" / f"{lifetime_id}.json"
        launch = self.read_json(launch_path)
        self.assertFalse(any(arg.startswith("notify=") for arg in launch["argv"]))

        dismissed = self.captain("dismiss", "Jack", "--confirm-unlanded")
        self.assertEqual(dismissed.returncode, 0, dismissed.stdout + dismissed.stderr)
        self.assertIn("Dismissed Jack", dismissed.stdout)
        manifest = self.read_json(manifest_path)
        self.assertEqual(manifest["deleted_ids"], [session_id])
        self.assertEqual(manifest["state"], "complete")
        launch = self.read_json(launch_path)
        self.assertIs(type(launch["native_returncode"]), int)
