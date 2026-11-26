"""A stdio Grok ACP fake with controllable prompts, permissions, and usage totals."""

import json
import os
import select
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_GROK_DIR"])
LOG = ROOT / "log.jsonl"
CONTROL = ROOT / "control"
SESSION = "session-1"


def record(entry):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def send(message):
    os.write(1, (json.dumps(message) + "\n").encode())


class Server:
    def __init__(self):
        self.prompt = None
        self.requests = 0
        self.model = None
        self.usage = {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}

    def answer(self, message):
        method, identifier = message["method"], message["id"]
        if method == "initialize":
            result = {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "agentInfo": {"name": "grok", "version": "1.0.62"},
            }
        elif method == "session/new":
            result = {"sessionId": SESSION}
        elif method == "session/set_model":
            self.model = message["params"]["modelId"]
            result = {}
        elif method == "session/prompt":
            if self.prompt is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32000, "message": "busy"},
                    }
                )
                return
            self.prompt = identifier
            return
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": f"unknown {method}"},
                }
            )
            return
        send({"jsonrpc": "2.0", "id": identifier, "result": result})

    def notification(self, message):
        if message["method"] == "session/cancel" and self.prompt is not None:
            identifier, self.prompt = self.prompt, None
            send({"jsonrpc": "2.0", "id": identifier, "result": {"stopReason": "cancelled"}})

    def response(self, message):
        record({"permission_response": message})

    def complete(self, command):
        if self.prompt is None:
            return
        reply = command.get("reply", "Done.")
        update = {
            "sessionId": SESSION,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": reply},
            },
        }
        send({"jsonrpc": "2.0", "method": "session/update", "params": update})
        self.usage["inputTokens"] += command.get("input", 10)
        self.usage["outputTokens"] += command.get("output", 2)
        self.usage["costUsd"] += command.get("cost", 0.01)
        usage = {
            "sessionId": SESSION,
            "update": {
                "sessionUpdate": "usage_update",
                "usage": {**self.usage, "model": self.model},
            },
        }
        send({"jsonrpc": "2.0", "method": "session/update", "params": usage})
        identifier, self.prompt = self.prompt, None
        send({"jsonrpc": "2.0", "id": identifier, "result": {"stopReason": "end_turn"}})

    def permission(self, command):
        self.requests += 1
        call = {
            "toolCallId": f"tool-{self.requests}",
            "title": command["command"],
            "kind": "execute",
            "rawInput": {"command": command["command"]},
        }
        params = {
            "sessionId": SESSION,
            "toolCall": call,
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
        }
        send(
            {
                "jsonrpc": "2.0",
                "id": f"permission-{self.requests}",
                "method": "session/request_permission",
                "params": params,
            }
        )

    def control(self, command):
        if command["do"] == "complete":
            self.complete(command)
        elif command["do"] == "permission":
            self.permission(command)
        elif command["do"] == "resume":
            self.usage = {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}
        record({"control": command})


def main():
    record({"argv": sys.argv[1:]})
    server, buffer = Server(), b""
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
                elif "method" in message:
                    server.notification(message)
                else:
                    server.response(message)
        for path in sorted(CONTROL.glob("*.json")):
            command = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
            server.control(command)


if __name__ == "__main__":
    main()
