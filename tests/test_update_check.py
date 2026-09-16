import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from captain_barbossa.update_check import check_for_update


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
        run.assert_called_once_with(["uv", "tool", "upgrade", "captain-barbossa"])

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


if __name__ == "__main__":
    unittest.main()
