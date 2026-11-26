# Supervisor socket contract

`captain supervise [--session ID]` runs one long-running process per session for a desktop app.
It is the one daemon Captain Barbossa has, and nothing in a Herdr session starts or loads it.
This page is the wire contract for the app and for the CLI's own door, `supervision.py`.
Design and phasing are in [terminal-free-plan.md](terminal-free-plan.md).

## Startup

- `supervise` creates a session (or opens `--session ID`) with no pane and records `"transport": "supervisor"` in `session.json`.
  A session that belongs to a Herdr workspace is refused with `[supervisor-herdr-session]`.
- It holds `supervisor.lock` in the session directory for its whole life; `supervision.alive` probes that flock, never a PID.
  A second supervisor for the same session is refused with `[supervisor-running]`.
- It listens on `~/.captain/run/s-<first 12 of session id>.sock` (`[supervisor] socket_dir`), directory 0700, socket 0600.
  The path is refused when relative, under `/tmp` or the OS temp directory (crew scratch), or 104 bytes or longer.
- It prints exactly one line to stdout and keeps running:

```json
{"type": "ready", "session_id": "<32 hex>", "socket": "/Users/me/.captain/run/s-<id12>.sock", "token": "<secret>", "pid": 4242}
```

- The token exists only in memory and on that line; it is never written to disk.
  The app passes it to the captain as `CAPTAIN_SUPERVISOR_TOKEN`, and the supervisor strips that variable from every process it spawns.
- SIGINT, SIGTERM, SIGHUP or the `shutdown` method stop every crew, remove the socket and exit.
  A client disconnecting (a closed app window) changes nothing; crew keep running.
- If the supervisor dies, the lifetime guard kills its crew; the next CLI poll closes their assignments as lost (`[supervisor-lost]`).

## Wire format

- One newline-terminated JSON-RPC 2.0 request per connection, then one response; lines are capped at 4 MiB.
- `params` is always an object and carries `"token"`; the supervisor removes it before dispatch.
- Every request is bounded: the request read and the reply write each get `[supervisor] request_timeout`.
  Only a supervised crew's turn has no total deadline.

```json
{"jsonrpc": "2.0", "id": 1, "method": "state", "params": {"token": "<secret>", "crew": "jack"}}
```

Every method returns a JSON object.
Every refusal is a JSON-RPC error whose message starts with its protocol code and whose `data.code` repeats it:

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "[supervisor-crew] jack is not running under this supervisor.", "data": {"code": "supervisor-crew"}}}
```

| JSON-RPC code | When |
| --- | --- |
| -32600 | malformed line or envelope (`jsonrpc-malformed`) |
| -32601 | unknown method (`unknown-method`) |
| -32001 | wrong or missing token (`supervisor-token`) |
| -32000 | every other refusal; a CaptainError without its own code becomes `supervisor-error` |

## Methods

| Method | Params | Result |
| --- | --- | --- |
| `start` | `crew`, `lifetime_ref` | `{crew, state: "started"}` |
| `stop` | `crew`, `reason` | `{crew, outcome, detail}`; outcome `stopped`, `retained`, `pending` or `not-running` |
| `shutdown` | none | `{crews: [stop result, ...]}`, then the supervisor exits |
| `state` | `crew` | `{crew, state}`; state `idle`, `working`, `blocked` or `null` |
| `ring` | `crew`, `message_id`, `text` | `{crew, landed, gate}`; gate `null`, `"agent not idle"` or `"delivery unconfirmed"` |
| `interrupt` | `crew`, `reason` | `{crew, outcome}`; outcome `interrupted` or `idle` |
| `interrupt_all` | `reason` (optional) | `{crews: [{crew, outcome, detail?}, ...]}`; outcome `interrupted`, `idle`, `error` or `pending` |
| `set_model` | `crew`, `model` | `{crew, model, applies}`; applies `now` or `next-turn` |
| `resolve_grant` | `request`, `decision` (`grant` or `deny`), `reason` (required to deny) | `{request, crew, state, message_id}`; state `granted` or `denied` |

The CLI calls `start`, `ring`, `stop`, `state`, `interrupt` and `set_model` itself, from `crew`, `tell`, `wait`, `dismiss`, `status` and `model`.
An app calls `interrupt_all`, `resolve_grant` and `shutdown` directly.

- `start` refuses a lifetime that is not the crew's own recorded launch, a crew not marked `"execution": "supervised"`, a provider with no supervised transport, and any start once `shutdown` began.
  It refuses with `[codex-hooks-missing]` when Codex does not load all four captain hooks (SessionStart, Stop, PreToolUse, PermissionRequest; Codex 0.164.0 or newer), and with `[codex-hooks-untrusted]` when any of them is untrusted, modified or disabled.
  The user fixes that once by approving the hooks in a Herdr Codex crew's review screen; Captain never writes Codex trust.
- `interrupt_all` interrupts every running crew in parallel and records `interrupted` in session memory for each confirmed one.
- `ring` with a `message_id` Codex already accepted lands again without starting a second turn.
  A `turn/start` Codex never answered reports `"delivery unconfirmed"`: held, never bounced, and settled by the next turn notice.

Refusal codes by method: `supervisor-params` (any), `supervisor-crew`, `supervisor-transport`, `supervisor-closing`, `supervisor-startup`, `codex-hooks-missing`, `codex-hooks-untrusted`, `claude-sdk-missing`, `claude-start` (start), `grant-request-unknown`, `grant-request-closed`, `grant-request-reason`, `grant-request-decision`, `grant-request-unsupported` (resolve_grant).

## Codex crew

- The crew runs `codex app-server` with the same `-c` instructions, hook declarations and `history.persistence="none"` as a pane crew, minus `notify`, `--sandbox`, `--ask-for-approval`, `--add-dir` and `-m`.
- `thread/start` sets `cwd` to the execution root, `sandbox: "workspace-write"`, `approvalPolicy: "on-request"` and the model.
  `turn/start` carries the rendered mail, the current model, and `sandboxPolicy.writableRoots` for owned paths outside the checkout.
- Command and file-change approval requests go through `captain hook` as PermissionRequest events: allow becomes `accept`, anything else `decline`, never `acceptForSession`.
- `item/tool/requestUserInput` is answered "use `captain ask`"; other server requests are refused.
- Native stderr is kept, capped at 1 MiB, in `sessions/<id>/supervisor/<crew>-<incarnation>.stderr`.

## Claude crew

- The crew runs `python -m captain_barbossa.claude_host CLAUDE <flags>`: the user's own `claude` with a pane crew's flags, driven through `ClaudeSDKClient` from the optional `claude-agent-sdk` (`pip install captain-barbossa[app]`).
  Without it, `start` is refused with `[claude-sdk-missing]`; a claude that fails to start is refused with `[claude-start]`.
- `--settings` (hooks, no attribution, additional directories) and `--session-id` reach claude through `extra_args`, never the SDK's own `settings` or `sandbox`.
  The system prompt is the `claude_code` preset with the crew instructions appended, the permission mode is `auto`, and setting sources are `user`, `project` and `local`, as for a pane crew.
- No `can_use_tool` callback is registered: the SDK races it against the PermissionRequest hook and the callback always wins.
  claude's own command hooks gate every call, and a call no hook answers is denied by claude itself.
- Claude stays hook-delivered: `ring` sends only the wake line as a query, and the UserPromptSubmit, Stop and SessionStart hooks hand over the body and stamp its receipt.
- `interrupt` and `set_model` use the SDK's own calls; a retier never writes the user's Claude settings.
  An interrupted or failed turn runs no Stop hook, so the supervisor records Claude's own idle notification shape (`Notification`, `idle_prompt`).
- An authentication failure posts one `[claude-auth]` notice on the assignment; the host never reads Claude credentials.
- On stop, the host closes claude; claude's exit of 0 or 143 after that close counts as a clean exit for native-history cleanup.

## Events

Each finished turn is appended to `events/<crew>-<incarnation>.jsonl` through `captain hook`, in Codex's own notify shape, so `wait`, `status` and usage read it as they read a pane crew's:

```json
{"type": "agent-turn-complete", "thread-id": "<thread>", "turn-id": "<turn>", "cwd": "/path/to/checkout", "input-messages": ["<mail text>"], "last-assistant-message": "<reply>", "provider": "codex", "incarnation_id": "<incarnation>", "seq": 7, "timestamp": "<iso>"}
```

A relayed approval is recorded like any hook decision, as `hook_event_name: "PermissionDecision"` with `approval: {verdict, code}`.
A supervised Claude crew's own hooks write its events, exactly as in a pane.
A supervised crew whose native CLI exits without a report is closed as lost with `[supervised-crew-exited]`.

## Grant requests

When the gate denies a supervised crew's call, the assignment in `protocol.json` keeps the latest denial:

```json
{"denial": {"tool": "Bash", "command": "git push origin main", "cwd": "/path/to/checkout", "code": "flagged-outward", "seq": 12}}
```

The crew's next `captain ask` turns it into a request on `assignment.grant_requests`:

```json
{
  "id": "<32 hex>",
  "crew": "jack",
  "assignment": "<assignment id>",
  "question": "<question id>",
  "tool": "Bash",
  "command": "git push origin main",
  "cwd": "/path/to/checkout",
  "code": "flagged-outward",
  "reason": "Flagged: leaves this machine (push, PR, publish, deploy, install, network write). ...",
  "category": "flagged",
  "words": "I need to push to main.",
  "state": "open",
  "at": 1795675819.4
}
```

- `category` is `flagged` (a `flagged-*` or `captain-verb` code), `not-granted` (any other deny) or `no-policy` (a deferred call).
- The question gets `"grant_request": "<id>"`, the captain's `asked` notice gains a line `[grant-request <id>] <code> (<category>): <command>`, and the `ask` response includes `grant_request`.
- `resolve_grant` claims the request (`state` `granted` or `denied`, `reason_given`, `resolved_at`).
  A grant adds one exact-command captain grant, spent on its first match and never inherited by a handoff; only shell calls can be granted.
  Either decision answers the pending question with mail the crew reads: `Granted once: run exactly this command again, unchanged: ...` or `Denied: <reason> ...`.

## Settings

`[supervisor]` in `defaults.toml`, left out of the `captain init` template: `request_timeout` 10, `startup_timeout` 30, `stop_timeout` 15, `hook_timeout` 30 (seconds), `socket_dir` `~/.captain/run`.

## CLI refusals in a supervised session

- `crew` refuses `--background`, `--placement`, `--direction` and `--split-pane` (`[supervised-flags]`) and providers without a supervised transport (`[supervised-transport]`).
- `focus` is refused (`[supervised-focus]`): focus belongs to the app.
- Prune never removes a supervised session whose supervisor still holds its lock.
