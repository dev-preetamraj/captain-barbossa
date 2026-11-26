"""A stdio stand-in for `codex app-server` that records every message it receives.

It runs as the crew's native process under the real lifetime guard. FAKE_CODEX_DIR holds
`log.jsonl` (argv, then every inbound message) and `control/`, where a test drops one JSON
command per file: complete the live turn, or ask for a command approval.
"""

import json
import os
import re
import select
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_CODEX_DIR"])
LOG = ROOT / "log.jsonl"
CONTROL = ROOT / "control"
THREAD = os.environ.get("FAKE_CODEX_THREAD_ID", "thread-1")
# FAKE_CODEX_TRUST sets the trust status hooks/list reports for every declared hook.
TRUST = os.environ.get("FAKE_CODEX_TRUST", "trusted")
RESUME = os.environ.get("FAKE_CODEX_RESUME", "accept")
# With FAKE_CODEX_SESSION_ID set, the first turn runs the declared SessionStart hook, as Codex
# does as its first turn begins, reporting this session id.
SESSION_ID = os.environ.get("FAKE_CODEX_SESSION_ID")
HOOK = re.compile(r'hooks\.(\w+)=\[\{hooks=\[\{type="command",command=("(?:[^"\\]|\\.)*")')


def declared_hooks(argv):
    """hooks/list rows for the -c hooks.<Event> declarations on this argv, as Codex lists them."""
    hooks = []
    for arg in argv:
        if match := HOOK.match(arg):
            event = match[1][0].lower() + match[1][1:]
            hooks.append(
                {"eventName": event, "command": json.loads(match[2]), "trustStatus": TRUST}
            )
    return hooks


def record(entry):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def send(message):
    os.write(1, (json.dumps(message) + "\n").encode())


class Server:
    def __init__(self, argv):
        self.turn, self.turns, self.requests = None, 0, 0
        self.hooks = declared_hooks(argv)
        self.thread = THREAD

    def finish(self, status, reply=""):
        if self.turn is None:
            return
        if reply:
            item = {"type": "agentMessage", "id": f"msg-{self.turns}", "text": reply}
            send({"method": "item/completed", "params": {"threadId": self.thread, "item": item}})
        turn = {"id": self.turn, "status": status, "items": []}
        send({"method": "turn/completed", "params": {"threadId": self.thread, "turn": turn}})
        self.turn = None

    def session_start(self):
        payload = {
            "session_id": SESSION_ID,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": os.getcwd(),
        }
        for hook in self.hooks:
            if hook["eventName"] == "sessionStart":
                result = subprocess.run(
                    shlex.split(hook["command"]),
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                record({"hook": "sessionStart", "returncode": result.returncode})

    def answer(self, message):
        method, identifier = message["method"], message["id"]
        if method == "initialize":
            send({"id": identifier, "result": {"userAgent": "fake-codex"}})
        elif method == "hooks/list":
            cwd = message["params"]["cwds"][0]
            row = {"cwd": cwd, "hooks": self.hooks, "warnings": [], "errors": []}
            send({"id": identifier, "result": {"data": [row]}})
        elif method == "thread/start":
            send({"id": identifier, "result": {"thread": {"id": self.thread}}})
        elif method == "thread/resume":
            if RESUME == "refuse":
                send(
                    {
                        "id": identifier,
                        "error": {"code": -32602, "message": "recorded thread unavailable"},
                    }
                )
            else:
                self.thread = message["params"]["threadId"]
                send({"id": identifier, "result": {"thread": {"id": self.thread}}})
        elif method == "turn/start":
            if SESSION_ID and not self.turns:
                self.session_start()
            self.turns += 1
            self.turn = f"turn-{self.turns}"
            turn = {"id": self.turn, "status": "inProgress", "items": []}
            send({"id": identifier, "result": {"turn": turn}})
            send({"method": "turn/started", "params": {"threadId": self.thread, "turn": turn}})
        elif method == "turn/interrupt":
            send({"id": identifier, "result": {}})
            self.finish("interrupted")
        else:
            send({"id": identifier, "error": {"code": -32601, "message": f"unknown {method}"}})

    def control(self, command):
        if command["do"] == "complete":
            self.finish("completed", command.get("reply", "Done."))
            return
        self.requests += 1
        params = {
            "threadId": self.thread,
            "turnId": self.turn,
            "itemId": f"item-{self.requests}",
        }
        if command["do"] == "approve":
            params.update(command=command["command"], cwd=command["cwd"], startedAtMs=0)
            method = "item/commandExecution/requestApproval"
        else:
            params.update(isBlocking=True, questions=[{"id": "q1", "header": "Q", "question": "?"}])
            method = "item/tool/requestUserInput"
        send({"id": f"fake-{self.requests}", "method": method, "params": params})


def main():
    record({"argv": sys.argv[1:]})
    server, buffer = Server(sys.argv[1:]), b""
    while True:
        if select.select([0], [], [], 0.05)[0]:
            chunk = os.read(0, 65536)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                record({"in": message})
                if "method" in message and "id" in message:
                    server.answer(message)
        for path in sorted(CONTROL.glob("*.json")):
            command = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
            server.control(command)


if __name__ == "__main__":
    main()
