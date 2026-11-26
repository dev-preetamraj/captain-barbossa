"""`captain providers` reports Codex's trust of captain's hooks, read with hooks/list only."""

import contextlib
import shlex
import unittest
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import instructions, model_catalogue, provider_status, runtime
from captain_barbossa.runtime import CaptainError

COMMAND = shlex.join(
    [*instructions.hook_command(provider_status.TRUST_PROBE)[:-1], "--environment"]
)
EVENTS = ("sessionStart", "stop", "preToolUse", "permissionRequest")


class Peer:
    def __init__(self, hooks):
        self.hooks, self.asked = hooks, []

    def request(self, method, params, timeout):
        self.asked.append(method)
        return {"data": [{"hooks": self.hooks}]} if method == "hooks/list" else {}

    def notify(self, method, timeout):
        self.asked.append(method)


class CodexHookTrustTests(SessionCase):
    def trust(self, hooks):
        peer = Peer(hooks)

        @contextlib.contextmanager
        def native(argv, root, factory):
            self.argv = argv
            yield peer

        with patch.object(model_catalogue, "_native", side_effect=native):
            found = provider_status.codex_hooks("codex", self.project, 5)
        self.assertEqual(peer.asked, ["initialize", "initialized", "hooks/list"])
        return found

    def test_each_trust_state_is_reported_and_nothing_is_written(self):
        hook = {"command": COMMAND, "trustStatus": "trusted", "enabled": True}
        trusted = [{**hook, "eventName": event} for event in EVENTS]
        self.assertEqual(self.trust(trusted), {"state": "trusted", "detail": None})
        self.assertEqual(self.argv[:2], ["codex", "app-server"])
        untrusted = [{**row, "trustStatus": "untrusted"} for row in trusted]
        self.assertEqual(self.trust(untrusted)["state"], "untrusted")
        self.assertEqual(self.trust(trusted[:1])["state"], "missing")

    def test_a_probe_that_cannot_run_reads_as_unknown(self):
        @contextlib.contextmanager
        def broken(argv, root, factory):
            raise CaptainError("codex app-server did not start")
            yield

        with patch.object(model_catalogue, "_native", side_effect=broken):
            found = provider_status.codex_hooks("codex", self.project, 5)
        self.assertEqual(found["state"], "unknown")

    def test_only_an_installed_codex_row_carries_hooks(self):
        trusted = {"state": "trusted", "detail": None}
        with (
            patch.object(runtime, "executable", side_effect=lambda name: f"/bin/{name}"),
            patch.dict(provider_status.ACCOUNT_PROBES, {}, clear=True),
            patch.object(provider_status, "codex_hooks", return_value=trusted),
            patch.object(provider_status.processes, "capture", side_effect=CaptainError("x")),
        ):
            rows = {
                row["provider"]: row for row in provider_status.catalogue(self.project)["providers"]
            }
        self.assertEqual(rows["codex"]["hooks"], trusted)
        self.assertIsNone(rows["pi"]["hooks"])


if __name__ == "__main__":
    unittest.main()
