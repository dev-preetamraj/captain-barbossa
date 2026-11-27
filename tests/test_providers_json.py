from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
from unittest.mock import patch

from captain_barbossa import cli, config, models, provider_status, runtime, sessions
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class ProvidersJsonTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.object(runtime, "herdr", side_effect=AssertionError("no Herdr")))
        self.enterContext(
            patch.object(sessions, "session", side_effect=AssertionError("no session"))
        )

    def test_sessionless_cli_reports_missing_providers_without_creating_state(self) -> None:
        out = io.StringIO()
        with (
            patch.object(runtime, "executable", side_effect=CaptainError("not installed")),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(cli.main(["providers", "--json"]), 0)
        response = json.loads(out.getvalue())
        self.assertEqual(response["version"], 1)
        self.assertEqual([row["provider"] for row in response["providers"]], list(models.PROVIDERS))
        for row in response["providers"]:
            self.assertIs(row["installed"], False)
            self.assertIsNone(row["signed_in"])
            self.assertIsNone(row["account"])
            self.assertIsNone(row["attempt"])
            self.assertEqual(row["sign_in"], models.CAPABILITIES[row["provider"]]["sign_in"])
        self.assertFalse(list((self.root / "state").glob("**/sessions/*")))

    def test_one_provider_uses_the_shared_probe_and_native_account_reader(self) -> None:
        native = subprocess.CompletedProcess([], 0, "native 1.2\n", "")
        with (
            patch.object(runtime, "executable", return_value="native"),
            patch.object(provider_status.processes, "capture", return_value=native),
            patch.dict(
                provider_status.ACCOUNT_PROBES,
                {
                    "claude": lambda *args: {
                        "signed_in": True,
                        "account": {"email": "user@example.invalid"},
                    }
                },
            ),
        ):
            response = provider_status.catalogue(self.project, "claude")
        self.assertEqual(len(response["providers"]), 1)
        row = response["providers"][0]
        self.assertTrue(row["installed"])
        self.assertEqual(row["version"], "native 1.2")
        self.assertTrue(row["signed_in"])
        self.assertEqual(row["account"], {"email": "user@example.invalid"})

    def test_failed_account_probe_leaves_sign_in_unknown(self) -> None:
        def failure(*args: object) -> dict:
            raise CaptainError("account failed")

        native = subprocess.CompletedProcess([], 1, "bad version", "")
        with (
            patch.object(runtime, "executable", return_value="native"),
            patch.object(provider_status.processes, "capture", return_value=native),
            patch.dict(provider_status.ACCOUNT_PROBES, {"pi": failure}),
        ):
            row = provider_status.catalogue(self.project, "pi")["providers"][0]
        self.assertTrue(row["installed"])
        self.assertIsNone(row["version"])
        self.assertIsNone(row["signed_in"])
        self.assertEqual(row["detail"], "account failed")

    def test_deadline_returns_a_full_unknown_row(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def waiting(*args: object) -> dict:
            release.wait(1)
            return {"installed": True}

        with (
            patch.object(config, "number", return_value=0.001),
            patch.object(provider_status, "probe", side_effect=waiting),
        ):
            row = provider_status.catalogue(self.project, "codex")["providers"][0]
        self.assertIsNone(row["installed"])
        self.assertIsNone(row["checked_at"])
        self.assertIn("deadline", row["detail"])
        self.assertIn("signed_in", row)

    def test_catalogue_redacts_credentials_from_diagnostics(self) -> None:
        with (
            patch.dict(os.environ, {"CAPTAIN_SUPERVISOR_TOKEN": "private-value"}),
            patch.object(
                provider_status,
                "probe",
                return_value={"detail": "private-value", "account": {"api_key": "also-private"}},
            ),
        ):
            row = provider_status.catalogue(self.project, "codex")["providers"][0]
        self.assertEqual(row["detail"], "[redacted]")
        self.assertEqual(row["account"]["api_key"], "[redacted]")
