"""`captain providers` lists each provider's permission modes and marks the user's own default."""

import contextlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import model_catalogue, provider_status, runtime
from captain_barbossa.runtime import CaptainError

HELP = """Options:
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  --plugin-dir <path>                   Load plugins
"""


class Peer:
    def __init__(self, settings, requirements=None):
        self.settings, self.requirements, self.asked = settings, requirements, []

    def request(self, method, params, timeout):
        self.asked.append(method)
        if method == "config/read":
            return {"config": self.settings}
        if method == "configRequirements/read":
            return {"requirements": self.requirements}
        return {}

    def notify(self, method, timeout):
        self.asked.append(method)


def ids(rows):
    return [row["id"] for row in rows]


def default(rows):
    return [row["id"] for row in rows if row["default"]]


class ClaudePermissionModeTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.enterContext(patch.object(Path, "home", return_value=self.home))

    def modes(self, stdout=HELP, code=0):
        found = subprocess.CompletedProcess([], code, stdout, "")
        with patch.object(provider_status.processes, "capture", return_value=found) as capture:
            rows = provider_status.claude_modes("claude", self.project, 5)
        self.assertEqual(capture.call_args.args[1], ["claude", "--help"])
        return rows

    def write_settings(self, path: Path, mode: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": mode}}))

    def test_modes_come_from_the_installed_cli_with_manual_reported_as_default(self):
        rows = self.modes()
        self.assertEqual(
            ids(rows), ["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]
        )
        self.assertEqual(set(rows[0]), {"id", "name", "description", "default"})
        self.assertTrue(all(row["name"] and row["description"] for row in rows))

    def test_a_mode_the_cli_does_not_offer_is_left_out_and_a_new_one_is_added(self):
        text = HELP.replace('"auto",', "").replace('"plan"', '"plan", "fancy"')
        rows = self.modes(text)
        self.assertNotIn("auto", ids(rows))
        self.assertEqual(
            rows[-1], {"id": "fancy", "name": "fancy", "description": "", "default": False}
        )

    def test_an_unreadable_help_falls_back_to_the_table(self):
        self.assertEqual(len(self.modes("no choices here")), len(provider_status.CLAUDE_MODES))
        self.assertEqual(len(self.modes(HELP, code=1)), len(provider_status.CLAUDE_MODES))

    def test_default_is_claudes_default_mode_when_no_settings_choose(self):
        self.assertEqual(default(self.modes()), ["default"])

    def test_default_follows_user_settings_and_project_settings_win(self):
        self.write_settings(self.home / ".claude" / "settings.json", "acceptEdits")
        self.assertEqual(default(self.modes()), ["acceptEdits"])
        self.write_settings(self.project / ".claude" / "settings.json", "plan")
        self.assertEqual(default(self.modes()), ["plan"])
        self.write_settings(self.project / ".claude" / "settings.local.json", "auto")
        self.assertEqual(default(self.modes()), ["auto"])

    def test_malformed_settings_are_skipped(self):
        user = self.home / ".claude" / "settings.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text("{not json")
        self.assertEqual(default(self.modes()), ["default"])


class CodexPermissionModeTests(SessionCase):
    def modes(self, settings, requirements=None):
        peer = Peer(settings, requirements)

        @contextlib.contextmanager
        def native(argv, root, factory):
            self.argv = argv
            yield peer

        with patch.object(model_catalogue, "native_peer", side_effect=native):
            rows = provider_status.codex_modes("codex", self.project, 5)
        self.assertEqual(
            peer.asked, ["initialize", "initialized", "config/read", "configRequirements/read"]
        )
        return rows

    def test_unset_config_selects_codexs_own_default_preset(self):
        rows = self.modes({"approval_policy": None, "sandbox_mode": None})
        self.assertEqual(ids(rows), ["read-only", "auto", "auto-review", "full-access"])
        self.assertEqual(default(rows), ["auto"])
        self.assertEqual(self.argv[:2], ["codex", "app-server"])

    def test_config_approval_and_sandbox_select_the_matching_preset(self):
        full = {"approval_policy": "never", "sandbox_mode": "danger-full-access"}
        self.assertEqual(default(self.modes(full)), ["full-access"])
        read = {"sandbox_mode": "read-only"}
        self.assertEqual(default(self.modes(read)), ["read-only"])
        review = {"approvals_reviewer": "guardian_subagent"}
        self.assertEqual(default(self.modes(review)), ["auto-review"])

    def test_a_default_permissions_profile_selects_its_sandbox(self):
        self.assertEqual(default(self.modes({"default_permissions": ":read-only"})), ["read-only"])

    def test_a_combination_no_preset_matches_marks_nothing(self):
        self.assertEqual(default(self.modes({"approval_policy": "untrusted"})), [])
        self.assertEqual(default(self.modes({"approval_policy": {"granular": {}}})), [])

    def test_requirements_remove_presets_they_do_not_allow(self):
        limits = {"allowedSandboxModes": ["read-only", "workspace-write"]}
        self.assertEqual(ids(self.modes({}, limits)), ["read-only", "auto", "auto-review"])
        limits = {"allowedApprovalPolicies": ["never"]}
        self.assertEqual(ids(self.modes({}, limits)), ["full-access"])

    def test_an_app_server_that_cannot_start_lists_presets_with_no_default(self):
        @contextlib.contextmanager
        def broken(argv, root, factory):
            raise CaptainError("codex app-server did not start")
            yield

        with patch.object(model_catalogue, "native_peer", side_effect=broken):
            rows = provider_status.codex_modes("codex", self.project, 5)
        self.assertEqual(len(rows), len(provider_status.CODEX_PRESETS))
        self.assertEqual(default(rows), [])


class PermissionModeRowTests(SessionCase):
    def catalogue(self, **modes):
        def read(provider, binary, root, timeout):
            return modes.get(provider, [])

        with (
            patch.object(runtime, "executable", side_effect=lambda name: f"/bin/{name}"),
            patch.dict(provider_status.ACCOUNT_PROBES, {}, clear=True),
            patch.object(provider_status, "codex_hooks", return_value=None),
            patch.object(provider_status.processes, "capture", side_effect=CaptainError("x")),
            patch.object(provider_status, "permission_modes", side_effect=read),
        ):
            rows = provider_status.catalogue(self.project)["providers"]
        return {row["provider"]: row for row in rows}

    def test_each_installed_row_carries_its_modes_and_pi_an_empty_list(self):
        mode = {"id": "plan", "name": "Plan", "description": "x", "default": False}
        rows = self.catalogue(claude=[mode])
        self.assertEqual(rows["claude"]["permission_modes"], [mode])
        self.assertEqual(rows["pi"]["permission_modes"], [])

    def test_pi_has_no_modes_and_an_uninstalled_provider_reports_null(self):
        self.assertEqual(provider_status.permission_modes("pi", "pi", self.project, 5), [])
        with patch.object(runtime, "executable", side_effect=CaptainError("not installed")):
            rows = provider_status.catalogue(self.project)["providers"]
        self.assertTrue(all(row["permission_modes"] is None for row in rows))


if __name__ == "__main__":
    unittest.main()
