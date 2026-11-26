"""One supervised Claude crew: the user's claude under the Agent SDK, driven by JSON-RPC on stdio.

Requests are connect, query, interrupt, set_model and close; notifications are turn and attention.
Mail, gating and usage stay with claude's own command hooks, and no permission callback is
registered because the SDK races one against the PermissionRequest hook and it always wins.
"""

import asyncio
import os
import signal
import sys
import threading

from . import jsonrpc
from .runtime import CaptainError

SDK_MISSING = (
    "[claude-sdk-missing] Supervised Claude crew need the Agent SDK: "
    "install captain-barbossa[app], then recruit again."
)
SETTING_SOURCES = ("user", "project", "local")
# claude's exit after a close we started: clean, or the SIGTERM the SDK or the guard sent.
CLOSED = (0, 143, -signal.SIGTERM)


def options(sdk, argv, cwd):
    """(ClaudeAgentOptions, session id) for a pane crew's claude argv; every flag but the system
    prompt and permission mode passes through extra_args, so options.settings stays unset."""
    cli, flags = argv[0], argv[1:]
    extra = {flag[2:]: value for flag, value in zip(flags[::2], flags[1::2])}
    if (
        len(flags) % 2
        or len(extra) * 2 != len(flags)
        or not all(flag.startswith("--") for flag in flags[::2])
    ):
        raise CaptainError(f"[claude-host] Expected claude, then unique --flag value pairs: {argv}")
    prompt = {"type": "preset", "preset": "claude_code"}
    if "append-system-prompt" in extra:
        prompt["append"] = extra.pop("append-system-prompt")
    typed = {"permission_mode": extra.pop("permission-mode")} if "permission-mode" in extra else {}
    built = sdk.ClaudeAgentOptions(
        cli_path=cli,
        system_prompt=prompt,
        extra_args=extra,
        setting_sources=list(SETTING_SOURCES),
        cwd=cwd,
        **typed,
    )
    return built, extra.get("session-id")


class Host:
    """`working` follows claude's own messages, so a turn claude starts by itself counts too."""

    def __init__(self, argv, out):
        self.argv, self.out = argv, out
        self.sdk = self.client = self.process = None
        self.working = self.warned = False
        self.exited = None
        self.stop = asyncio.Event()
        self.tasks = set()

    def send(self, message):
        jsonrpc.write(self.out, {"jsonrpc": "2.0", **message})

    def notify(self, method, **params):
        self.send({"method": method, "params": params})

    def read(self, loop):
        try:
            reader = jsonrpc.LineReader(0)
            while (message := reader.read()) is not None:
                loop.call_soon_threadsafe(self.dispatch, message)
        except (CaptainError, OSError) as exc:
            print(f"claude_host: {exc}", file=sys.stderr)
        finally:
            loop.call_soon_threadsafe(self.stop.set)

    def dispatch(self, message):
        if "id" in message and "method" in message:
            task = asyncio.create_task(self.answer(message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def answer(self, message):
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        errors = (CaptainError, OSError, *((self.sdk.ClaudeSDKError,) if self.sdk else ()))
        try:
            reply = {"result": await self.call(message["method"], params)}
        except errors as exc:
            text = str(exc) if str(exc).startswith("[") else f"[claude-host] {exc}"
            reply = {"error": {"code": -32000, "message": text}}
        self.send({"id": message["id"], **reply})

    async def call(self, method, params):
        if method == "connect":
            return await self.connect()
        if self.client is None:
            raise CaptainError(f"[claude-host] {method} before connect.")
        match method:
            case "query":
                if not isinstance(params.get("text"), str):
                    raise CaptainError("[claude-host] query needs text.")
                self.working = True
                await self.client.query(params["text"])
                return {}
            case "interrupt":
                if not self.working:
                    return {"outcome": "idle"}
                await self.client.interrupt()
                return {"outcome": "interrupted"}
            case "set_model":
                await self.client.set_model(params.get("model"))
                return {}
            case "close":
                self.stop.set()
                return {}
        raise CaptainError(f"[claude-host] Unknown method {method}.")

    async def connect(self):
        if self.client is not None:
            raise CaptainError("[claude-host] Already connected.")
        try:
            import claude_agent_sdk as sdk  # optional [app] extra; nothing else loads it
        except ImportError as exc:
            raise CaptainError(SDK_MISSING) from exc
        self.sdk = sdk
        built, session_id = options(sdk, self.argv, os.getcwd())
        client = sdk.ClaudeSDKClient(built)
        try:
            await client.connect()
        except (sdk.ClaudeSDKError, OSError) as exc:
            raise CaptainError(f"[claude-start] claude did not start: {exc}") from exc
        self.client = client
        # The SDK drops its handle on disconnect, and claude's code decides history cleanup.
        self.process = getattr(getattr(client, "_transport", None), "_process", None)
        self.tasks.add(asyncio.create_task(self.pump()))
        return {"session_id": session_id}

    async def pump(self):
        code = 1
        try:
            async for message in self.client.receive_messages():
                self.observe(message)
            code = 0
        except self.sdk.ProcessError as exc:
            code = exc.exit_code if isinstance(exc.exit_code, int) else 1
        except (self.sdk.ClaudeSDKError, OSError) as exc:
            print(f"claude_host: {exc}", file=sys.stderr)
        finally:
            if not self.stop.is_set():
                self.exited = code
                self.stop.set()

    def observe(self, message):
        if isinstance(message, self.sdk.ResultMessage):
            self.working = False
            if getattr(message, "api_error_status", None) == 401:
                self.attention()
            # Claude runs no Stop hook for an interrupted or failed turn.
            clean = message.subtype == "success" and not message.is_error
            self.notify("turn", state="idle", clean=clean)
        elif isinstance(message, self.sdk.AssistantMessage):
            if getattr(message, "error", None) == "authentication_failed":
                self.attention()
            if not self.working:
                self.working = True
                self.notify("turn", state="working")

    def attention(self):
        if not self.warned:
            self.warned = True
            self.notify("attention", code="claude-auth")

    async def run(self):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.stop.set)
        threading.Thread(target=self.read, args=(loop,), daemon=True).start()
        await self.stop.wait()
        if self.client is not None and self.exited is None:
            try:
                await self.client.disconnect()
            except (self.sdk.ClaudeSDKError, OSError) as exc:
                print(f"claude_host: {exc}", file=sys.stderr)
        return self.returncode()

    def returncode(self):
        """claude's own exit code; one that follows our own close, by TERM or end of input, is 0."""
        if self.exited is not None:
            return self.exited
        code = getattr(self.process, "returncode", None) if self.client else 0
        if code in CLOSED:
            return 0
        return code if isinstance(code, int) and code > 0 else 1


def main(argv):
    if not argv:
        raise SystemExit("usage: python -m captain_barbossa.claude_host CLAUDE [--flag value ...]")
    # Replies own fd 1; anything else that prints, here or in a child, lands on stderr.
    out = os.dup(1)
    os.dup2(2, 1)

    async def serve():
        return await Host(argv, out).run()

    return asyncio.run(serve())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
