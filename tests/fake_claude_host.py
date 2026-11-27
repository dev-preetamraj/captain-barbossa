"""A stand-in claude_agent_sdk whose client runs the --settings command hooks for real.

Run as a script it installs itself as claude_agent_sdk and runs the real claude_host as the crew's
native process. FAKE_CLAUDE_DIR holds `log.jsonl` (connect options, every call, every hook and
its output) and `control/`, where a test drops one JSON command per file.
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("FAKE_CLAUDE_DIR", "."))


class ClaudeSDKError(Exception):
    pass


class CLIConnectionError(ClaudeSDKError):
    pass


class ProcessError(ClaudeSDKError):
    def __init__(self, message, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


class ClaudeAgentOptions:
    def __init__(self, **given):
        self.given = given
        self.__dict__.update(given)


@dataclass
class AssistantMessage:
    content: list = field(default_factory=list)
    model: str = "fake"
    error: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    is_error: bool = False
    api_error_status: int | None = None


def record(entry):
    with (ROOT / "log.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


class ClaudeSDKClient:
    def __init__(self, options):
        self.options, self._transport, self.turn, self.tools = options, None, False, 0

    async def connect(self):
        record({"connect": self.options.given})
        self.messages = asyncio.Queue()
        self._transport = type("Transport", (), {})()
        self._transport._process = type("Process", (), {"returncode": None})()
        await self.hook("SessionStart", source="startup")

    async def hook(self, event, **fields):
        settings = json.loads(self.options.extra_args["settings"])
        payload = {
            "hook_event_name": event,
            "session_id": self.options.extra_args.get("session-id"),
            "cwd": self.options.cwd,
            **fields,
        }
        for group in settings.get("hooks", {}).get(event, ()):
            for hook in group["hooks"]:
                result = await asyncio.to_thread(
                    subprocess.run,
                    shlex.split(hook["command"]),
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                output = json.loads(result.stdout) if result.stdout.strip() else None
                record({"hook": event, "fields": fields, "output": output})

    async def query(self, prompt):
        record({"query": prompt})
        self.turn = True
        await self.hook("UserPromptSubmit", prompt=prompt)

    async def interrupt(self):
        record({"interrupt": True})
        if self.turn:
            self.turn = False
            await self.messages.put(ResultMessage("error_during_execution", is_error=True))

    async def set_model(self, model):
        record({"set_model": model})

    async def disconnect(self):
        record({"disconnect": True})
        self._transport._process.returncode = 143
        await self.messages.put(None)

    async def receive_messages(self):
        while True:
            for path in sorted((ROOT / "control").glob("*.json")):
                command = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
                await self.control(command)
            try:
                message = self.messages.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue
            if message is None:
                return
            if isinstance(message, ProcessError):
                raise message
            yield message

    async def control(self, command):
        match command["do"]:
            case "complete":
                await self.hook("Stop", stop_hook_active=False)
                self.turn = False
                await self.messages.put(AssistantMessage([command.get("reply", "Done.")]))
                await self.messages.put(ResultMessage("success"))
            case "tool":
                self.tools += 1
                tool = {"command": command["command"]}
                await self.hook(
                    "PreToolUse", tool_name="Bash", tool_input=tool, tool_use_id=f"t{self.tools}"
                )
            case "auth":
                self.turn = False
                await self.messages.put(AssistantMessage(error="authentication_failed"))
                await self.messages.put(ResultMessage("success", True, api_error_status=401))
            case "exit":
                await self.messages.put(ProcessError("claude exited", command["code"]))


if __name__ == "__main__":
    missing = os.environ.get("FAKE_CLAUDE_SDK") == "missing"
    sys.modules["claude_agent_sdk"] = None if missing else sys.modules[__name__]
    from captain_barbossa import claude_host

    sys.exit(claude_host.main(sys.argv[1:]))
