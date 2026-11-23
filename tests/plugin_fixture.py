"""A real plugin executable for tests, scripted by a JSON file beside it.

Each call appends its request to `<name>.log` and answers from `<name>.json`, keyed by the
request's `call`. A reply may carry `_sleep` seconds or an `_exit` status instead of JSON,
and a `_raw` string is written to stdout verbatim.
"""

import json
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from captain_barbossa import config

GOLDEN = Path(__file__).resolve().parent.parent / "docs" / "plugin-protocol"

SCRIPT = """#!{python}
import json, pathlib, sys, time
here = pathlib.Path(__file__)
spec = json.loads(here.with_suffix(".json").read_text())
request = json.load(sys.stdin)
with open(here.with_suffix(".log"), "a") as log:
    log.write(json.dumps(request) + "\\n")
reply = spec[request["call"]]
time.sleep(reply.pop("_sleep", 0))
if "_exit" in reply:
    sys.exit(reply["_exit"])
sys.stdout.write(reply["_raw"] if "_raw" in reply else json.dumps(reply))
"""


def golden(name):
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def hello(name="example", context=(1,), required=True, commands=(), protocol=(1,), **extra):
    response = {
        "ok": True,
        "name": name,
        "protocol": list(protocol),
        "points": {"context": {"versions": list(context), "required": required}},
        **extra,
    }
    if commands:
        response["points"]["crew_denials"] = {
            "versions": [1],
            "required": True,
            "commands": [list(command) for command in commands],
        }
    return response


def snapshot(captain="captain context\n", crew="crew context\n", *, days=30, server="host"):
    files = []
    if captain is not None:
        files.append({"path": "context/captain.md", "roles": ["captain"], "text": captain})
    if crew is not None:
        files.append({"path": "context/crew.md", "roles": ["crew"], "text": crew})
    expires = datetime.now(UTC) + timedelta(days=days)
    return {
        "ok": True,
        "server_id": server,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "files": files,
    }


class FakePlugin:
    """One scripted plugin executable in `directory`."""

    def __init__(self, directory, name="example"):
        self.name = name
        self.path = Path(directory) / name
        self.path.write_text(SCRIPT.format(python=sys.executable), encoding="utf-8")
        self.path.chmod(0o755)
        self.script(hello=hello(name), sync=snapshot())

    def script(self, **replies):
        spec_path = self.path.with_suffix(".json")
        spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
        spec.update(replies)
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def requests(self):
        log = self.path.with_suffix(".log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


def register(*plugins, required=()):
    """Write home settings registering each plugin, as `captain plugin add` would."""
    path = Path.home() / ".captain" / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        f'[plugins.{plugin.name}]\ncommand = "{plugin.path}"\n'
        f"required = {'true' if plugin.name in required else 'false'}\n"
        for plugin in plugins
    )
    path.write_text(text, encoding="utf-8")
    tomllib.loads(text)
    clear_settings()


def unregister():
    (Path.home() / ".captain" / "settings.toml").unlink(missing_ok=True)
    clear_settings()


def clear_settings():
    config.settings.cache_clear()
