"""The app lists providers, rechecks them and starts sign-in: Codex through its own app-server
account calls, Claude, pi and Grok as guidance only."""

import contextlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import config, models, runtime, store, supervisor
from captain_barbossa.runtime import CaptainError

KEY = "sk-test-0123456789abcdef"
FAKE = r"""
import json, os, select, sys, time
from pathlib import Path

root = Path(os.environ["FAKE_PROVIDER_DIR"])
name, args = Path(sys.argv[0]).name, sys.argv[1:]


def log(entry):
    with (root / f"{name}.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


log({"argv": args})
if args == ["--version"]:
    print({"codex": "codex-cli 0.172.0", "grok": "grok 1.0.62", "claude": "2.1.332 (Claude Code)",
           "pi": "1.6.0"}[name])
elif name == "claude":
    print((root / "claude-status.json").read_text())
elif name == "pi":
    print("provider   model              context  max-out  thinking  images")
    print("anthropic  claude-sonnet-5-5  200K     64K      yes       yes")
else:
    login, buffer = None, b""

    def send(message):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()

    while True:
        done = root / "codex-done"
        if login and done.exists():
            success = json.loads(done.read_text())
            done.unlink()
            if success:
                (root / "account.json").write_text(json.dumps({"type": "chatgpt", "email": "a@b",
                                                               "planType": "plus"}))
            send({"method": "account/login/completed",
                  "params": {"loginId": login, "success": success, "error": None}})
        if not select.select([0], [], [], 0.05)[0]:
            continue
        chunk = os.read(0, 65536)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            message = json.loads(line)
            log({"in": message})
            if "id" not in message:
                continue
            params, method = message.get("params") or {}, message["method"]
            result = {}
            if method == "account/read":
                found = root / "account.json"
                result = {"account": json.loads(found.read_text()) if found.exists() else None,
                          "requiresOpenaiAuth": True}
            elif method == "account/login/start" and params["type"] == "apiKey":
                if params["apiKey"].endswith("bad"):
                    send({"id": message["id"], "error": {"code": -32000,
                          "message": "rejected key " + params["apiKey"]}})
                    continue
                (root / "account.json").write_text(json.dumps({"type": "apiKey"}))
                result = {"type": "apiKey"}
            elif method == "account/login/start":
                login = "login-1"
                result = {"type": "chatgptDeviceCode", "loginId": login, "userCode": "WXYZ-1234",
                          "verificationUrl": "https://auth.example/device"}
            elif method == "account/login/cancel":
                login = None
            send({"id": message["id"], "result": result})
"""


class ProviderStatusTests(SessionCase):
    def setUp(self):
        super().setUp()
        settings = self.project / ".captain"
        settings.mkdir()
        store.write_text(
            settings / "settings.toml",
            "[supervisor]\nrequest_timeout=5.0\nstartup_timeout=10.0\nstop_timeout=5.0\n",
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.addCleanup(models.pi_models.cache_clear)
        self.enterContext(contextlib.chdir(self.project))
        self.fake = self.root / "fake"
        self.fake.mkdir()
        script = self.root / "fake_provider.py"
        script.write_text(f"#!{sys.executable}\n{FAKE}")
        script.chmod(0o700)
        # One script for every CLI: it reads which one it plays from the link it runs as.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in models.PROVIDERS:
            (self.bin / name).symlink_to(script)
        self.installed = set(models.PROVIDERS)
        self.enterContext(
            patch.object(runtime, "executable", side_effect=self.executable),
        )
        env = {"FAKE_PROVIDER_DIR": str(self.fake)}
        self.enterContext(patch.dict(os.environ, env))
        self.claude_status({"loggedIn": True, "authMethod": "claude.ai", "email": "me@x"})
        current = supervisor.open_session(self.project, None)
        self.supervisor = supervisor.Supervisor(current, "token", threading.Event())
        self.feed = supervisor.join(self.supervisor)
        self.addCleanup(supervisor.cancel_sign_ins, self.supervisor)

    def executable(self, name):
        if name not in self.installed:
            raise CaptainError(f"{name} is not installed or is missing from PATH.")
        return str(self.bin / name)

    def claude_status(self, status):
        (self.fake / "claude-status.json").write_text(json.dumps(status))

    def rows(self, **params):
        found = supervisor.providers(self.supervisor, params)["providers"]
        return {row["provider"]: row for row in found}

    def log(self, name):
        path = self.fake / f"{name}.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line) for line in lines]

    def attentions(self):
        found = []
        with contextlib.suppress(queue.Empty):
            while True:
                method, params = self.feed.get_nowait()
                if method == "attention":
                    found.append(params)
        return found

    def until(self, check, what):
        deadline = time.monotonic() + 10
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)
        return found

    def test_every_provider_reports_install_version_and_sign_in(self):
        self.installed.discard("grok")
        rows = self.rows()
        self.assertEqual(list(rows), list(models.PROVIDERS))
        codex = rows["codex"]
        self.assertEqual((codex["installed"], codex["version"]), (True, "codex-cli 0.172.0"))
        self.assertIs(codex["signed_in"], False)
        self.assertEqual(codex["sign_in"]["methods"], ["chatgptDeviceCode", "apiKey"])
        claude = rows["claude"]
        self.assertIs(claude["signed_in"], True)
        self.assertEqual(claude["account"], {"type": "claude.ai", "email": "me@x"})
        self.assertIn("claude auth login", claude["sign_in"]["guidance"])
        self.assertEqual((rows["pi"]["signed_in"], rows["pi"]["account"]), (True, {"models": 1}))
        grok = rows["grok"]
        self.assertEqual(
            (grok["installed"], grok["version"], grok["signed_in"]), (False, None, None)
        )
        self.assertIn("not installed", grok["detail"])

    def test_status_is_cached_until_a_recheck(self):
        self.rows()
        self.claude_status({"loggedIn": False})
        self.assertIs(self.rows()["claude"]["signed_in"], True)
        self.assertIs(self.rows(recheck=True, provider="claude")["claude"]["signed_in"], False)
        for params in ({"recheck": "yes"}, {"provider": "gpt"}):
            with self.subTest(params=params), self.assertRaisesRegex(CaptainError, "params"):
                supervisor.providers(self.supervisor, params)

    def test_claude_status_comes_only_from_its_own_cli(self):
        self.rows(provider="claude")
        self.assertIn({"argv": ["auth", "status", "--json"]}, self.log("claude"))
        self.claude_status({"unexpected": True})
        unknown = self.rows(recheck=True, provider="claude")["claude"]
        self.assertIsNone(unknown["signed_in"])

    def test_claude_pi_and_grok_sign_in_is_guidance_only(self):
        for provider, hint in (
            ("claude", "ANTHROPIC_API_KEY"),
            ("pi", "pi auth login"),
            ("grok", "Herdr session"),
        ):
            with self.subTest(provider=provider):
                reply = supervisor.sign_in(self.supervisor, {"provider": provider})
                self.assertEqual(reply["state"], "guidance")
                self.assertIn(hint, reply["guidance"])
        self.assertEqual(self.log("claude"), [])

    def test_a_codex_device_code_is_surfaced_then_signed_in(self):
        reply = supervisor.sign_in(self.supervisor, {"provider": "codex"})
        self.assertEqual(
            (reply["state"], reply["user_code"], reply["verification_url"]),
            ("waiting", "WXYZ-1234", "https://auth.example/device"),
        )
        with self.assertRaisesRegex(CaptainError, r"\[sign-in-running\]"):
            supervisor.sign_in(self.supervisor, {"provider": "codex"})
        (self.fake / "codex-done").write_text("true")
        attempt = self.supervisor.sign_ins["codex"]
        self.until(attempt.done.is_set, "the sign-in to finish")
        self.assertEqual(attempt.state, "signed-in")
        self.assertIs(self.rows()["codex"]["signed_in"], True)
        states = [notice["sign_in"]["state"] for notice in self.attentions()]
        self.assertEqual(states, ["waiting", "signed-in"])

    def test_a_cancelled_codex_sign_in_cancels_its_login(self):
        supervisor.sign_in(self.supervisor, {"provider": "codex"})
        reply = supervisor.cancel_sign_in(self.supervisor, {"provider": "codex"})
        self.assertEqual(reply["state"], "cancelled")
        requests = [entry["in"]["method"] for entry in self.log("codex") if "in" in entry]
        self.assertIn("account/login/cancel", requests)
        again = supervisor.cancel_sign_in(self.supervisor, {"provider": "codex"})
        self.assertEqual(again, {"provider": "codex", "state": "not-running"})

    def test_a_codex_api_key_goes_to_codex_and_nowhere_else(self):
        reply = supervisor.sign_in(
            self.supervisor, {"provider": "codex", "method": "apiKey", "api_key": KEY}
        )
        attempt = self.supervisor.sign_ins["codex"]
        self.until(attempt.done.is_set, "the key sign-in")
        self.assertEqual(attempt.state, "signed-in")
        sent = [entry["in"] for entry in self.log("codex") if "in" in entry]
        login = next(message for message in sent if message["method"] == "account/login/start")
        self.assertEqual(login["params"], {"type": "apiKey", "apiKey": KEY})
        self.assertNotIn(KEY, json.dumps([reply, attempt.view(), self.attentions()]))
        self.assertNotIn(KEY, "".join(path.read_text() for path in self.state_files()))

    def test_a_rejected_api_key_is_redacted_from_the_failure(self):
        bad = KEY + "bad"
        supervisor.sign_in(
            self.supervisor, {"provider": "codex", "method": "apiKey", "api_key": bad}
        )
        attempt = self.supervisor.sign_ins["codex"]
        self.until(attempt.done.is_set, "the refused key")
        self.assertEqual(attempt.state, "failed")
        self.assertIn("rejected key", attempt.detail)
        self.assertNotIn(bad, json.dumps([attempt.view(), self.attentions()]))

    def test_a_bad_sign_in_request_is_refused(self):
        for params, code in (
            ({"provider": "codex", "method": "deviceAuth"}, "sign-in-method"),
            ({"provider": "codex", "method": "apiKey"}, "supervisor-params"),
            ({"provider": "gpt"}, "supervisor-params"),
        ):
            with self.subTest(params=params), self.assertRaisesRegex(CaptainError, code):
                supervisor.sign_in(self.supervisor, params)
        self.installed.discard("codex")
        with self.assertRaisesRegex(CaptainError, r"\[sign-in-missing\]"):
            supervisor.sign_in(self.supervisor, {"provider": "codex"})

    def test_hello_lists_the_routes_and_their_sign_in_methods(self):
        hello = supervisor.hello(self.supervisor, {})
        for method in ("providers", "sign_in", "cancel_sign_in", "usage"):
            self.assertIn(method, hello["methods"])
        self.assertEqual(hello["capabilities"]["grok"]["sign_in"]["methods"], [])
        self.assertEqual(hello["capabilities"]["claude"]["sign_in"]["methods"], [])

    def state_files(self):
        return [path for path in Path(self.root / "state").rglob("*") if path.is_file()]
