"""`captain settings --json` and the supervisor `settings` method: the merged settings, the
layer that set each key and the two user files, read with no session."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import cli, config, store, supervision, supervisor


class SettingsJsonTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.enterContext(patch.object(config.Path, "home", return_value=self.home))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(contextlib.chdir(self.project))
        for root in (self.home, self.project):
            (root / config.SETTINGS_PATH).parent.mkdir(parents=True)
        store.write_text(
            self.home / config.SETTINGS_PATH, '[dashboard]\ninterval = 3.0\n[crew]\nmodel = "mid"\n'
        )
        store.write_text(
            self.project / config.SETTINGS_PATH,
            '[crew]\nmodel = "strong"\n[supervisor]\nrequest_timeout = 5.0\n',
        )
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(list(argv)), 0)
        return out.getvalue()

    def test_json_names_each_keys_layer_and_both_user_files(self):
        found = json.loads(self.run_cli("settings", "--json"))
        self.assertEqual(found["version"], 1)
        self.assertEqual(found["settings"]["crew"]["model"], "strong")
        self.assertEqual(found["settings"]["dashboard"]["interval"], 3.0)
        self.assertEqual(found["settings"], config.settings())
        self.assertEqual(
            {key: found["sources"][key] for key in ("crew.model", "dashboard.interval")},
            {"crew.model": "project", "dashboard.interval": "home"},
        )
        self.assertEqual(found["sources"]["captain.model"], "defaults")
        leaves = {".".join(names) for names, _ in config.leaves(found["settings"])}
        self.assertEqual(set(found["sources"]), leaves)
        self.assertEqual(
            found["paths"],
            {
                "home": str(self.home / config.SETTINGS_PATH),
                "project": str(self.project / config.SETTINGS_PATH),
            },
        )

    def test_plain_output_is_one_line_per_key_with_its_layer(self):
        lines = self.run_cli("settings").splitlines()
        self.assertIn('crew.model = "strong"  (project)', lines)
        self.assertIn("dashboard.interval = 3.0  (home)", lines)

    def test_a_running_supervisor_returns_the_same_read(self):
        sockets = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockets, ignore_errors=True)
        self.enterContext(
            patch.object(
                supervision,
                "socket_path",
                side_effect=lambda directory: sockets / f"s-{directory.name[:12]}.sock",
            )
        )
        os.environ.pop(supervision.TOKEN_ENV, None)
        current = supervisor.open_session(self.project, None)
        read_fd, write_fd = os.pipe()
        stopped = threading.Event()

        def target():
            with os.fdopen(write_fd, "w") as out:
                supervisor.serve(current, out, stopped)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(stopped.set)
        with os.fdopen(read_fd) as lines:
            os.environ[supervision.TOKEN_ENV] = json.loads(lines.readline())["token"]
        self.assertEqual(supervision.request(current.directory, "settings"), config.described())


if __name__ == "__main__":
    unittest.main()
