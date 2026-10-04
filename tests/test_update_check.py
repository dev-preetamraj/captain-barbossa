import contextlib
import importlib.metadata
import io
import os
import tomllib
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from captain_barbossa import cli
from captain_barbossa.update_check import UPGRADE_COMMAND, check_for_update


class UpdateCheckTests(unittest.TestCase):
    def _urlopen(self, version):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = f'{{"info": {{"version": "{version}"}}}}'.encode()
        return response

    @patch("captain_barbossa.update_check.subprocess.run")
    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_prompts_and_upgrades_when_a_newer_release_exists(self, urlopen, confirm, run):
        urlopen.return_value = self._urlopen("0.16.0")
        confirm.return_value.ask.return_value = True
        check_for_update()
        confirm.assert_called_once()
        self.assertIn("0.16.0", confirm.call_args.args[0])
        self.assertIn("Update now?", confirm.call_args.args[0])
        self.assertNotIn("uv tool upgrade", confirm.call_args.args[0])
        run.assert_called_once_with(UPGRADE_COMMAND)

    @patch("captain_barbossa.update_check.subprocess.run")
    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_declining_the_prompt_skips_the_upgrade(self, urlopen, confirm, run):
        urlopen.return_value = self._urlopen("0.16.0")
        confirm.return_value.ask.return_value = False
        check_for_update()
        run.assert_not_called()

    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_up_to_date_never_prompts(self, urlopen, confirm):
        urlopen.return_value = self._urlopen("0.15.3")
        check_for_update()
        confirm.assert_not_called()

    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_network_failure_never_raises_or_prompts(self, urlopen, confirm):
        urlopen.side_effect = urllib.error.URLError("no network")
        check_for_update()
        confirm.assert_not_called()

    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_malformed_response_never_raises_or_prompts(self, urlopen, confirm):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"not json"
        urlopen.return_value = response
        check_for_update()
        confirm.assert_not_called()

    @patch("captain_barbossa.update_check.subprocess.run", side_effect=FileNotFoundError("uv"))
    @patch("captain_barbossa.update_check.questionary.confirm")
    @patch("captain_barbossa.update_check.urllib.request.urlopen")
    @patch("captain_barbossa.update_check.__version__", "0.15.3")
    def test_missing_uv_during_the_prompt_never_raises(self, urlopen, confirm, run):
        urlopen.return_value = self._urlopen("0.16.0")
        confirm.return_value.ask.return_value = True
        check_for_update()
        run.assert_called_once()


class UpdateCommandTests(unittest.TestCase):
    def test_parser_accepts_update(self):
        self.assertEqual(cli.parser().parse_args(["update"]).command, "update")

    def test_upgrades_without_a_workspace(self):
        with (
            patch("captain_barbossa.update_check.subprocess.run") as run,
            patch.object(cli, "current_pane") as pane,
        ):
            run.return_value.returncode = 0
            code = cli.main(["update"])
        self.assertEqual(code, 0)
        pane.assert_not_called()
        run.assert_called_once_with(UPGRADE_COMMAND)

    def test_returns_uv_exit_code(self):
        with patch("captain_barbossa.update_check.subprocess.run") as run:
            run.return_value.returncode = 2
            self.assertEqual(cli.main(["update"]), 2)

    def test_missing_uv_is_a_captain_error(self):
        with (
            patch(
                "captain_barbossa.update_check.subprocess.run",
                side_effect=FileNotFoundError("uv"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(["update"])
        self.assertEqual(code, 1)
        self.assertIn("uv is not installed or is missing from PATH.", err.getvalue())

    def test_crew_may_not_update(self):
        with (
            patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}),
            patch("captain_barbossa.update_check.subprocess.run") as run,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(["update"])
        self.assertEqual(code, 1)
        self.assertIn("captain-only", err.getvalue())
        run.assert_not_called()


class VersionTests(unittest.TestCase):
    def test_version_flag_reports_the_package_metadata_version(self):
        expected = importlib.metadata.version("captain-barbossa")
        with open(Path(__file__).parents[1] / "pyproject.toml", "rb") as handle:
            self.assertEqual(tomllib.load(handle)["project"]["version"], expected)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_info:
                cli.main(["--version"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(output.getvalue(), f"captain {expected}\n")
        self.assertEqual(cli.__version__, expected)


if __name__ == "__main__":
    unittest.main()
