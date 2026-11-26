"""One supervised Claude crew: the user's claude under the Agent SDK, driven by JSON-RPC on stdio.

Requests are connect, query, interrupt, set_model and close; notifications are turn, result,
message and attention. Supervised mail, gating and usage stay with claude's own command hooks;
only a captain's host (connect `ask`) registers a callback, asking the supervisor `permission`,
and isolated one-shot turns a deny-all one, which wins the SDK's PermissionRequest race.
"""

import asyncio
import dataclasses
import json
import os
import signal
import sys
import threading

from . import attachments, jsonrpc
from .runtime import CaptainError

SDK_MISSING = (
    "[claude-sdk-missing] A supervised Claude captain or crew needs the Agent SDK: "
    "install captain-barbossa[app], then start it again."
)
SETTING_SOURCES = ("user", "project", "local")
# claude's exit after a close we started: clean, or the SIGTERM the SDK or the guard sent.
NO_PERSISTENCE = "--no-session-persistence"
CLOSED = (0, 143, -signal.SIGTERM)
# Outlasts the supervisor's own wait for the user's answer, which declines when it expires.
ASK_TIMEOUT = 1860
# Base64 image bytes per prompt, kept well inside the Messages API's 32 MB request limit.
IMAGE_BUDGET = 20 * 1024 * 1024


def options(sdk, argv, cwd, *, isolated=False, partial=False, can_use_tool=None, effort=None):
    """Build native options, stripping executable user configuration for isolated turns."""
    cli, flags = argv[0], argv[1:]
    ephemeral = isolated and NO_PERSISTENCE in flags
    if isolated:
        flags = [flag for flag in flags if flag != NO_PERSISTENCE]
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
    if isolated:

        async def deny_tool(tool_name, tool_input, context):
            return sdk.PermissionResultDeny(message="Background crew are read-only.")

        settings = json.loads(extra.get("settings", "{}"))
        hook = settings.get("hooks", {}).get("PreToolUse", [])
        extra["settings"] = json.dumps({"hooks": {"PreToolUse": hook}})
        extra["strict-mcp-config"] = None
        if ephemeral:
            extra[NO_PERSISTENCE[2:]] = None
        extra["mcp-config"] = json.dumps({"mcpServers": {}})
        typed.update(
            permission_mode="dontAsk",
            env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
            allowed_tools=["Read", "Grep", "Glob"],
            tools=["Read", "Grep", "Glob"],
            can_use_tool=deny_tool,
            mcp_servers={},
        )
    if partial:
        typed["include_partial_messages"] = True
    if can_use_tool is not None:
        typed["can_use_tool"] = can_use_tool
    if effort is not None:
        typed["effort"] = effort
    built = sdk.ClaudeAgentOptions(
        cli_path=cli,
        system_prompt=prompt,
        extra_args=extra,
        setting_sources=[] if isolated else list(SETTING_SOURCES),
        cwd=cwd,
        **typed,
    )
    return built, extra.get("session-id")


class Host:
    """`working` follows claude's own messages, so a turn claude starts by itself counts too."""

    def __init__(self, argv, out):
        self.argv, self.out = argv, out
        self.sdk = self.client = self.process = self.model = None
        self.working = self.warned = False
        self.exited = None
        self.stop = asyncio.Event()
        self.tasks = set()
        self.waiting, self.next_id = {}, 0

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
        elif (future := self.waiting.get(message.get("id"))) is not None and not future.done():
            future.set_result(message)

    async def request(self, method, params, timeout):
        """The supervisor's reply to a request of ours, or None past `timeout`."""
        self.next_id += 1
        identifier = f"host-{self.next_id}"
        future = self.waiting[identifier] = asyncio.get_running_loop().create_future()
        try:
            self.send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except (CaptainError, OSError, TimeoutError):
            return None
        finally:
            self.waiting.pop(identifier, None)

    async def ask(self, tool_name, tool_input, context):
        """The captain's permission prompt, decided by the user in the app; unanswered denies.

        Claude's own allow-rule suggestions go to the app, and a `remember` answer has claude
        keep exactly those rules, as the terminal's don't-ask-again choice does.
        """
        rules = [
            update
            for update in context.suggestions or ()
            if update.type == "addRules" and update.behavior == "allow"
        ]
        reply = await self.request(
            "permission",
            {"tool": tool_name, "input": tool_input, "rules": plain(rules)},
            ASK_TIMEOUT,
        )
        result = (reply or {}).get("result")
        result = result if isinstance(result, dict) else {}
        if result.get("behavior") == "allow":
            updated = dict(tool_input)
            if isinstance(result.get("answers"), dict):
                updated["answers"] = result["answers"]
            remembered = rules if result.get("remember") is True and rules else None
            return self.sdk.PermissionResultAllow(
                updated_input=updated, updated_permissions=remembered
            )
        message = result.get("message") or "Declined: nobody answered in the app."
        return self.sdk.PermissionResultDeny(message=message)

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
            return await self.connect(ask=params.get("ask") is True, effort=params.get("effort"))
        if self.client is None:
            raise CaptainError(f"[claude-host] {method} before connect.")
        match method:
            case "query":
                if not isinstance(params.get("text"), str):
                    raise CaptainError("[claude-host] query needs text.")
                self.working = True
                attached = params.get("attachments") or []
                prompt = await asyncio.to_thread(user_prompt, params["text"], attached)
                await self.client.query(prompt)
                return {}
            case "interrupt":
                if not self.working:
                    return {"outcome": "idle"}
                await self.client.interrupt()
                return {"outcome": "interrupted"}
            case "set_model":
                try:
                    if "model" in params or "effort" not in params:
                        await self.client.set_model(params.get("model"))
                    if "effort" in params:
                        await self.set_effort(params["effort"])
                except CaptainError:
                    raise
                except Exception as exc:  # the SDK raises bare Exception for a refused control
                    raise CaptainError(f"[claude-model] claude refused the change: {exc}") from exc
                return {}
            case "close":
                self.stop.set()
                return {}
        raise CaptainError(f"[claude-host] Unknown method {method}.")

    async def set_effort(self, effort):
        """Claude's own /effort path for a running session: its flag-settings layer."""
        send = getattr(getattr(self.client, "_query", None), "_send_control_request", None)
        if send is None:
            raise CaptainError(
                "[effort-unsupported] This Agent SDK cannot change a running Claude's effort."
            )
        await send({"subtype": "apply_flag_settings", "settings": {"effortLevel": effort}})

    async def connect(self, ask=False, effort=None):
        if self.client is not None:
            raise CaptainError("[claude-host] Already connected.")
        try:
            import claude_agent_sdk as sdk  # optional [app] extra; nothing else loads it
        except ImportError as exc:
            raise CaptainError(SDK_MISSING) from exc
        self.sdk = sdk
        built, session_id = options(
            sdk,
            self.argv,
            os.getcwd(),
            partial=True,
            can_use_tool=self.ask if ask else None,
            effort=effort,
        )
        client = sdk.ClaudeSDKClient(built)
        try:
            await client.connect()
        except (sdk.ClaudeSDKError, OSError) as exc:
            raise CaptainError(f"[claude-start] claude did not start: {exc}") from exc
        self.client = client
        # The SDK drops its handle on disconnect, and claude's code decides history cleanup.
        self.process = getattr(getattr(client, "_transport", None), "_process", None)
        self.tasks.add(asyncio.create_task(self.pump()))
        info = getattr(client, "get_server_info", None)
        info = await info() if info is not None else None
        commands = info.get("commands") if isinstance(info, dict) else None
        return {"session_id": session_id, "commands": plain(commands or [])}

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
            self.notify(
                "result",
                usage=plain(getattr(message, "usage", None)),
                total_cost_usd=plain(getattr(message, "total_cost_usd", None)),
                model_usage=plain(getattr(message, "model_usage", None)),
                model=self.model,
            )
            # Claude runs no Stop hook for an interrupted or failed turn.
            clean = message.subtype == "success" and not message.is_error
            self.notify("turn", state="idle", clean=clean)
            return
        if isinstance(message, self.sdk.AssistantMessage):
            if getattr(message, "error", None) == "authentication_failed":
                self.attention()
            if isinstance(getattr(message, "model", None), str):
                self.model = message.model
            if not self.working:
                self.working = True
                self.notify("turn", state="working")
        self.output(message)

    def output(self, message):
        """Every other SDK message, partial deltas included, for the app's live output."""
        try:
            self.notify("message", kind=type(message).__name__, data=plain(message))
        except CaptainError:
            pass  # an oversized message is dropped; the transcript keeps it whole

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


def user_prompt(text, attached):
    """The text alone, or one user message streamed with its images ahead of the text."""
    text, images = attachments.prompt(text, attached, IMAGE_BUDGET)
    if not images:
        return text
    blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": record["mime"],
                "data": attachments.encoded(data),
            },
        }
        for record, data in images
    ]
    message = {
        "type": "user",
        "message": {"role": "user", "content": [*blocks, {"type": "text", "text": text}]},
        "parent_tool_use_id": None,
    }

    async def stream():
        yield message

    return stream()


def plain(value):
    """An SDK message as JSON values, each dataclass tagged with its class name."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {item.name: plain(getattr(value, item.name)) for item in dataclasses.fields(value)}
        return {"type": type(value).__name__, **fields}
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def one_shot(argv, out):
    """Emit the native result shape after one isolated turn and disconnect before exiting."""
    import claude_agent_sdk as sdk

    built, _ = options(sdk, argv[:-1], os.getcwd(), isolated=True)
    client = sdk.ClaudeSDKClient(built)
    try:
        await client.connect()
        await client.query(argv[-1])
        async for message in client.receive_response():
            if isinstance(message, sdk.ResultMessage):
                payload = {
                    "type": "result",
                    "subtype": message.subtype,
                    "is_error": message.is_error,
                    "result": message.result,
                    "num_turns": message.num_turns,
                    "total_cost_usd": message.total_cost_usd,
                }
                jsonrpc.write(out, payload)
                return 0 if message.subtype == "success" and not message.is_error else 1
        return 1
    finally:
        await client.disconnect()


def main(argv):
    if not argv:
        raise SystemExit("usage: python -m captain_barbossa.claude_host CLAUDE [--flag value ...]")
    # Replies own fd 1; anything else that prints, here or in a child, lands on stderr.
    out = os.dup(1)
    os.dup2(2, 1)

    async def serve():
        if argv[0] == "--one-shot":
            return await one_shot(argv[1:], out)
        return await Host(argv, out).run()

    return asyncio.run(serve())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
