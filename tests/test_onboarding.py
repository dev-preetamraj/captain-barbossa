"""Onboarding: captain bootstraps Herdr instead of erroring out of the launch path."""

import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, onboarding
from captain_barbossa.runtime import CaptainError

CREATED = {"root_pane": {"pane_id": "wZ:p1"}}
OUTSIDE = {key: "" for key in ("HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID")}


class Recorder:
    """Stands in for runtime.herdr, replaying canned results per subcommand."""

    def __init__(self, list_failures=0):
        self.calls = []
        self.list_failures = list_failures

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("workspace", "list"):
            if self.list_failures:
                self.list_failures -= 1
                raise CaptainError("No such file or directory")
            return {"workspaces": []}
        if args[:2] == ("workspace", "create"):
            return CREATED
        return {}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    **OUTSIDE,
                    "CAPTAIN_PROJECT": str(self.project),
                    "CAPTAIN_SESSION": "",
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                },
            )
        )
        for name in ("CAPTAIN_STATE_ROOT", "CAPTAIN_TEMP_ROOT"):
            os.environ.pop(name, None)
        self.herdr = Recorder()
        self.enterContext(patch.object(onboarding.runtime, "herdr", self.herdr))
        self.enterContext(
            patch.object(onboarding.runtime, "executable", lambda name: f"/bin/{name}")
        )
        self.enterContext(patch.object(onboarding, "project_root", lambda: self.project))
        self.enterContext(patch("captain_barbossa.onboarding.sys.stdin.isatty", lambda: True))
        self.exec = self.enterContext(patch.object(onboarding.os, "execv"))
        self.popen = self.enterContext(patch.object(onboarding.subprocess, "Popen"))
        self.enterContext(patch.object(onboarding.time, "sleep", lambda seconds: None))

    def answer(self, value):
        confirm = self.enterContext(patch.object(onboarding.questionary, "confirm"))
        confirm.return_value.unsafe_ask.return_value = value
        return confirm

    def args(self, argv=()):
        return cli.parser().parse_args(list(argv))

    def herdr_call(self, *prefix):
        return next((call for call in self.herdr.calls if call[: len(prefix)] == prefix), None)

    def onboard_launcher_path(self):
        """The launcher path travels as an env var on `workspace create`, not interpolated
        into the `pane run` shell string, so a quote in it cannot inject commands."""
        create = self.herdr_call("workspace", "create")
        prefix = "CAPTAIN_ONBOARD_LAUNCHER="
        env_value = next(arg for arg in create if arg.startswith(prefix))
        return Path(env_value[len(prefix) :])

    def launcher_flags(self, run_command):
        """`pane run` only gets a fixed `/bin/sh "$CAPTAIN_ONBOARD_LAUNCHER"`; read the
        launcher (named via env) for the rest."""
        self.assertEqual(run_command, '/bin/sh "$CAPTAIN_ONBOARD_LAUNCHER"')
        path = self.onboard_launcher_path()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        exec_line = path.read_text(encoding="utf-8").splitlines()[1]
        tokens = shlex.split(exec_line)
        self.assertEqual(tokens[:4], ["exec", sys.executable, "-m", "captain_barbossa"])
        return tokens[4:]

    def test_opens_workspace_and_carries_launch_flags(self):
        self.answer(True)
        with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
            onboarding.bootstrap(
                self.args(["--agent", "claude", "--session", "s1", "--prompt", "hi"])
            )
        create = self.herdr_call("workspace", "create")
        self.assertIn("--cwd", create)
        self.assertEqual(create[create.index("--cwd") + 1], str(self.project))
        self.assertIn("--focus", create)
        self.assertIn("CAPTAIN_BOOTSTRAPPED=1", create)
        run = self.herdr_call("pane", "run")
        self.assertEqual(run[2], "wZ:p1")
        self.assertEqual(
            self.launcher_flags(run[3]),
            ["--agent", "claude", "--session", "s1", "--prompt", "hi"],
        )
        self.exec.assert_called_once_with("/bin/herdr", ["/bin/herdr"])

    def test_launcher_path_travels_as_env_not_interpolated_shell_text(self):
        """A quote in CAPTAIN_MEMORY_ROOT must not let the launcher path inject commands
        into the `pane run` shell string."""
        with patch.dict(
            os.environ, {"CAPTAIN_MEMORY_ROOT": str(self.root / 'state"; touch pwned; "')}
        ):
            self.answer(True)
            with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
                onboarding.bootstrap(self.args())
        run = self.herdr_call("pane", "run")
        self.assertEqual(run[3], '/bin/sh "$CAPTAIN_ONBOARD_LAUNCHER"')
        self.assertNotIn("pwned", run[3])
        self.assertTrue(self.onboard_launcher_path().is_file())

    def test_omits_flags_the_user_did_not_pass(self):
        self.answer(True)
        with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
            onboarding.bootstrap(self.args())
        self.assertEqual(self.launcher_flags(self.herdr_call("pane", "run")[3]), [])

    def test_pane_run_command_stays_short_regardless_of_prompt_size(self):
        """A >1KB command typed into a canonical-mode pane truncates and runs partial shell."""
        self.answer(True)
        long_prompt = "x" * 5000
        with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
            onboarding.bootstrap(self.args(["--prompt", long_prompt]))
        run = self.herdr_call("pane", "run")
        self.assertLess(len(run[3]), 200)
        self.assertEqual(self.launcher_flags(run[3]), ["--prompt", long_prompt])

    def test_launcher_ignores_argv0_so_python_m_bootstraps(self):
        """`python -m captain_barbossa` leaves a non-executable argv[0]; the new pane
        exec'd it and died with the terminal already handed to Herdr."""
        self.answer(True)
        module = str(Path(onboarding.__file__).parent / "__main__.py")
        with patch.object(onboarding.sys, "argv", [module]):
            with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
                onboarding.bootstrap(self.args())
        launcher = self.onboard_launcher_path()
        exec_line = launcher.read_text(encoding="utf-8").splitlines()[1]
        self.assertNotIn(module, exec_line)
        self.assertEqual(self.launcher_flags(self.herdr_call("pane", "run")[3]), [])

    def test_starts_a_server_when_the_socket_does_not_answer(self):
        self.herdr.list_failures = 2
        self.answer(True)
        with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
            onboarding.bootstrap(self.args())
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args.args[0], ["/bin/herdr", "server"])
        # One spawn, however many polls it takes before the workspace is created.
        self.assertIsNotNone(self.herdr_call("workspace", "create"))

    def test_gives_up_when_the_server_never_answers(self):
        self.herdr.list_failures = 10**6
        with patch.object(onboarding.time, "monotonic", side_effect=[0, 0, 99, 99]):
            with self.assertRaises(CaptainError) as caught:
                onboarding.start_server()
        self.assertIn("did not start", str(caught.exception))

    def test_installs_herdr_then_resolves_it_off_a_stale_path(self):
        self.answer(True)
        calls = []
        installed = []

        def which(name):
            return f"/bin/{name}" if name != "herdr" or installed else None

        def run(command, **kwargs):
            calls.append(command)
            installed.append(True)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(onboarding.shutil, "which", which):
            with patch.object(onboarding.subprocess, "run", run), redirect_stdout(io.StringIO()):
                onboarding.bootstrap(self.args())
        self.assertEqual(calls[0][:2], ["curl", "-fsSL"])
        self.assertIn(onboarding.INSTALL_URL, calls[0])
        self.assertEqual(calls[1][0], "sh")
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[-1], onboarding.INSTALL_BIN)
        self.assertIsNotNone(self.herdr_call("workspace", "create"))

    def test_failed_download_reports_instead_of_opening_a_workspace(self):
        self.answer(True)
        with patch.object(onboarding.shutil, "which", lambda name: None):
            with (
                patch.object(
                    onboarding.subprocess,
                    "run",
                    lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
                ),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(CaptainError):
                    onboarding.bootstrap(self.args())
        self.assertEqual(self.herdr.calls, [])

    def test_failed_shell_after_a_successful_download_still_reports(self):
        """A pipe reports sh's exit code, not curl's; check each step's own status."""
        self.answer(True)

        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0 if command[0] == "curl" else 1)

        with patch.object(onboarding.shutil, "which", lambda name: None):
            with patch.object(onboarding.subprocess, "run", run), redirect_stdout(io.StringIO()):
                with self.assertRaises(CaptainError):
                    onboarding.bootstrap(self.args())
        self.assertEqual(self.herdr.calls, [])

    def test_install_appends_install_bin_after_existing_path(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}):
            with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
                with (
                    patch.object(
                        onboarding.subprocess,
                        "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    onboarding.install()
            self.assertEqual(os.environ["PATH"], f"/usr/bin{os.pathsep}{onboarding.INSTALL_BIN}")

    def test_install_leaves_no_empty_component_when_path_is_unset(self):
        with patch.dict(os.environ, {"PATH": ""}):
            with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
                with (
                    patch.object(
                        onboarding.subprocess,
                        "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    onboarding.install()
            self.assertEqual(os.environ["PATH"], onboarding.INSTALL_BIN)

    def test_inside_a_workspace_bootstrap_does_nothing(self):
        confirm = self.answer(True)
        pane = dict.fromkeys(OUTSIDE, "w1")
        with patch.dict(os.environ, pane):
            with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
                onboarding.bootstrap(self.args())
        confirm.assert_not_called()
        self.assertEqual(self.herdr.calls, [])

    def test_bootstrapped_env_short_circuits_before_asking(self):
        """The captain re-launched into the new pane must not bootstrap again."""
        confirm = self.answer(True)
        with patch.dict(os.environ, {"CAPTAIN_BOOTSTRAPPED": "1"}):
            with patch.object(onboarding.shutil, "which", lambda name: None):
                onboarding.bootstrap(self.args())
        confirm.assert_not_called()
        self.assertEqual(self.herdr.calls, [])

    def run_cli(self, argv=()):
        """Declining or running headless must keep the pre-onboarding error messages."""
        stderr = io.StringIO()
        with patch.object(onboarding.shutil, "which", lambda name: f"/bin/{name}"):
            with redirect_stderr(stderr):
                code = cli.main(list(argv))
        return code, stderr.getvalue().strip()

    def test_declining_keeps_todays_error(self):
        self.answer(False)
        self.assertEqual(self.run_cli(), (1, "captain: Run captain inside a Herdr workspace."))
        self.assertEqual(self.herdr.calls, [])

    def test_non_tty_keeps_todays_error_without_asking(self):
        confirm = self.answer(True)
        with patch("captain_barbossa.onboarding.sys.stdin.isatty", lambda: False):
            self.assertEqual(self.run_cli(), (1, "captain: Run captain inside a Herdr workspace."))
        confirm.assert_not_called()

    def test_subcommands_never_bootstrap(self):
        confirm = self.answer(True)
        self.assertEqual(
            self.run_cli(["crew", "--task", "x"]),
            (1, "captain: Run captain inside a Herdr workspace."),
        )
        confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
