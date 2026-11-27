"""A stdio pi RPC fake that records commands and runs the generated gate hook."""

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ["FAKE_PI_DIR"])
LOG = ROOT / "log.jsonl"
CONTROL = ROOT / "control"
HOOK = re.compile(r"const HOOK = (\[[^;]+\]);")
CONFIG = re.compile(r"const \{ root, events, incarnation \} = (\{[^;]+\});")


def record(entry):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def send(message):
    os.write(1, (json.dumps(message) + "\n").encode())


def option(argv, name):
    index = argv.index(name)
    return argv[index + 1]


class Server:
    def __init__(self, argv):
        self.argv = argv
        self.streaming = False
        self.state_error = None
        self.prompts = 0
        self.model = option(argv, "--model")
        self.session_id = option(argv, "--session-id")
        self.session_dir = Path(option(argv, "--session-dir"))
        source = Path(option(argv, "--extension")).read_text(encoding="utf-8")
        self.hook = json.loads(HOOK.search(source).group(1))
        self.config = json.loads(CONFIG.search(source).group(1))
        self.transcript = self.session_dir / f"fake-{self.session_id}.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        event = {
            "provider": "pi",
            "incarnation_id": self.config["incarnation"],
            "type": "session_start",
            "session_id": self.session_id,
            "transcript_path": str(self.transcript),
        }
        with Path(self.config["events"]).open("a", encoding="utf-8") as events:
            events.write(json.dumps(event) + "\n")

    def response(self, message, data=None, success=True, error=None):
        reply = {
            "id": message["id"],
            "type": "response",
            "command": message["type"],
            "success": success,
        }
        if data is not None:
            reply["data"] = data
        if error is not None:
            reply["error"] = error
        send(reply)

    def command(self, message):
        kind = message["type"]
        if kind == "extension_ui_response":
            return
        if kind == "get_state":
            if self.state_error is not None:
                error, self.state_error = self.state_error, None
                self.response(message, success=False, error=error)
                return
            self.response(
                message,
                {
                    "model": {"provider": self.model.partition("/")[0], "id": self.model},
                    "isStreaming": self.streaming,
                    "isCompacting": False,
                    "sessionId": self.session_id,
                    "messageCount": self.prompts,
                    "pendingMessageCount": 0,
                },
            )
        elif kind == "prompt":
            if self.streaming:
                self.response(message, success=False, error="already streaming")
                return
            self.prompts += 1
            self.streaming = True
            self.response(message, {"disposition": "started"})
            send({"type": "agent_start"})
        elif kind == "abort":
            was_streaming = self.streaming
            self.streaming = False
            self.response(message)
            if was_streaming:
                send({"type": "agent_settled", "aborted": True})
        elif kind == "set_model":
            self.model = f"{message['provider']}/{message['modelId']}"
            self.response(
                message,
                {"provider": message["provider"], "id": message["modelId"]},
            )
        else:
            self.response(message, success=False, error=f"unknown {kind}")

    def control(self, command):
        if command["do"] == "complete":
            if self.streaming:
                reply = command.get("reply", "Done.")
                message = {"role": "assistant", "content": [{"type": "text", "text": reply}]}
                send({"type": "message_end", "message": message})
                self.streaming = False
                send({"type": "agent_settled", "aborted": False})
        elif command["do"] == "state_error":
            self.state_error = command.get("error", "get_state failed")
        elif command["do"] == "streaming":
            self.streaming = command["value"]
        elif command["do"] == "extension_ui":
            send(
                {
                    "type": "extension_ui_request",
                    "id": command["id"],
                    "method": command["method"],
                }
            )
        elif command["do"] == "tool":
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "bash",
                "tool_input": {"command": command["command"]},
                "tool_use_id": f"tool-{time.monotonic_ns()}",
                "session_id": self.session_id,
                "cwd": command.get("cwd") or os.getcwd(),
            }
            result = subprocess.run(
                self.hook,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = json.loads(result.stdout) if result.stdout.strip() else None
            record({"hook": "PreToolUse", "output": output, "returncode": result.returncode})
        record({"control": command})


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
                server.command(message)
        for path in sorted(CONTROL.glob("*.json")):
            command = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
            server.control(command)


if __name__ == "__main__":
    main()
