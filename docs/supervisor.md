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
  `subscribe` is the one exception: after its response the connection stays open and carries notifications until the client closes it or the supervisor stops.
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
| `shutdown` | `end` (optional, default false) | `{crews: [stop result, ...], ended}`, then the supervisor exits |
| `state` | `crew` | `{crew, state}`; state `idle`, `working`, `blocked` or `null` |
| `ring` | `crew`, `message_id`, `text` | `{crew, landed, gate}`; gate `null`, `"agent not idle"` or `"delivery unconfirmed"` |
| `interrupt` | `crew`, `reason` | `{crew, outcome}`; outcome `interrupted` or `idle` |
| `interrupt_all` | `reason` (optional) | `{crews: [{crew, outcome, detail?}, ...]}`; outcome `interrupted`, `idle`, `error` or `pending` |
| `set_model` | `crew`, `model` | `{crew, model, applies}`; applies `now` or `next-turn` |
| `resolve_grant` | `request`, `decision` (`grant` or `deny`), `reason` (required to deny) | `{request, crew, state, message_id}`; state `granted` or `denied` |
| `hello` | none | `{protocol, version, session_id, project, capabilities, methods, notifications, commands}` |
| `command` | `argv` (list of strings, verb first) | `{exit, stdout, stderr}`, plus `truncated: true` when either output passed 1 MiB |
| `subscribe` | `cursors` (optional object: stream name to `{offset, generation}` or a bare offset) | `{session_id, cursors}`, then notifications on the same connection |
| `transcript` | `agent`, `after` or `before` (optional cursors), `limit` (1 to 500, default 200), `incarnation` (optional) | `{agent, provider, files, entries, start, end, more}` |
| `step` | `agent`, `id`, `incarnation` (optional) | one entry with its whole text |
| `rename` | `name` | `{session_id, name}` |
| `sessions` | none | the session list (see Session hub) |

The CLI calls `start`, `ring`, `stop`, `state`, `interrupt` and `set_model` itself, from `crew`, `tell`, `wait`, `dismiss`, `status` and `model`.
An app calls `hello`, `command`, `subscribe`, `transcript`, `step`, `rename`, `sessions`, `interrupt_all`, `resolve_grant` and `shutdown` directly.

- `start` refuses a lifetime that is not the crew's own recorded launch, a crew not marked `"execution": "supervised"`, a provider with no supervised transport, and any start once `shutdown` began.
  It refuses with `[codex-hooks-missing]` when Codex does not load all four captain hooks (SessionStart, Stop, PreToolUse, PermissionRequest; Codex 0.164.0 or newer), and with `[codex-hooks-untrusted]` when any of them is untrusted, modified or disabled.
  The user fixes that once by approving the hooks in a Herdr Codex crew's review screen; Captain never writes Codex trust.
- `interrupt_all` interrupts every running crew in parallel and records `interrupted` in session memory for each confirmed one.
- `ring` with a `message_id` Codex already accepted lands again without starting a second turn.
  A `turn/start` Codex never answered reports `"delivery unconfirmed"`: held, never bounced, and settled by the next turn notice.

Refusal codes by method: `supervisor-params` (any), `supervisor-crew`, `supervisor-transport`, `supervisor-closing`, `supervisor-startup`, `codex-hooks-missing`, `codex-hooks-untrusted`, `claude-sdk-missing`, `claude-start` (start), `grant-request-unknown`, `grant-request-closed`, `grant-request-reason`, `grant-request-decision`, `grant-request-unsupported` (resolve_grant), `command-refused`, `command-timeout`, `command-failed` (command), `transcript-agent`, `transcript-range`, `transcript-entry`, `transcript-file`, `transcript-gone` (transcript, step), `session-name` (rename).

### hello

`protocol` is `captain.supervisor/1`; a client refuses any other major version.
`capabilities` has one row per provider:

```json
{"codex": {"supervised": true, "transport": "app-server", "delivery": "body", "gate": "hooks", "output": "items", "limits": ["Needs Codex 0.164.0 or newer, ..."]}}
```

- `supervised` is whether this supervisor can start that provider's crew; `transport` is null when it cannot.
- `delivery` is `body` (the ring carries the rendered mail) or `hook` (the ring is a wake line and the crew's own hook hands the body over).
- `gate` is `hooks`, `extension` (pi) or `none` (grok: its own auto mode is the whole gate, so it fails open).
- `output` is `items` (Codex app-server item and turn notifications), `events` (pi RPC session events), `turns` (Claude: turn state only) or null.
- `limits` states each provider's known gaps in plain words.

### command

`argv[0]` must be one of `crew`, `assign`, `tell`, `answer`, `grant`, `interrupt`, `model`, `wait`, `status`, `dismiss`, `recover`, `memory`, `result`, `worktree`, `dashboard` (only as `dashboard --once`) or `inspect`.
`do` and `quiet` stay out until the app has an authorization flow.
The verb runs as `python -m captain_barbossa --session <id> ARGV...` in the project, as the captain: crew identity variables are dropped, `CAPTAIN_SESSION` and the supervisor token are set, stdin is empty.
The CLI stays the only writer of captain state, and the reply is the verb's own exit code and output; a non-zero exit is a result, not a refusal.
Each verb has its own deadline (`supervisor.COMMANDS`: 30 to 180 seconds, `wait` 600); past it the process group is killed and the call is refused with `[command-timeout]`.
Both pipes are drained as the verb runs and kept to 1 MiB plus a 4 KiB redaction margin each; the rest is read and dropped.
A client sets its own read deadline above the verb's, and passes `wait --timeout` below 600.

### subscribe

Streams are `actions` (the protocol action log) and one per native event file, named by its stem (`captain`, `<crew>-<incarnation>`).
A stream missing from `cursors` replays from 0, so `{}` replays the whole session.
A stream's `generation` names the file itself (device and inode); a cursor whose generation differs, or whose offset is past the file's end, replays that stream from 0 and its first notification carries `reset: true`, so the client drops what it held for the stream.
A bare-offset cursor has no generation and resets only past the end.
Notifications, all redacted (see below):

| Notification | Params | Replayed |
| --- | --- | --- |
| `event` | `{stream, crew, cursor, generation, reset, events}`: lines past the previous cursor, at most 1 MiB per notification; `crew` is null for `actions` and `captain` | yes, from `{offset: cursor, generation}` |
| `delivery` | one `wait --all` delivery: `{status, delivery_id, crew, assignment_id, summary}` | while unacknowledged |
| `output` | `{crew, method, params}`: a native notification as the provider sent it | no |
| `attention` | `{crew, code, message}`: a start refused (hooks missing or untrusted, SDK missing) or a native auth failure | no |

- A client keeps the latest `cursor` per stream and passes them to its next `subscribe`, so a reconnect misses no line and repeats none.
- `delivery` is the projection `wait --all` returns, sent once per subscription while its `delivery_id` stays pending; it never acknowledges, so the captain's own `wait` still receives and acks it.
- `output` and `attention` are live only, and a subscriber more than 1000 notifications behind loses the oldest ones rather than stalling a crew.

## Protocol actions

In a supervised session only, every protocol action is appended to `sessions/<id>/actions.jsonl` as it happens, by whichever process makes it (CLI, hook or supervisor), with a stable id.
A Herdr session writes no action log and reads nothing extra to decide that: callers pass the transport from metadata they already hold, and a delivery hook's receipt reads the `supervised` flag `begin` sets on the assignment.

- An append waits at most 1 second for `actions.lock`.
  An action made under the protocol lock (`ask`, a grant request, `done`, a lost crew), a busy log or a failed append goes to `actions.spool/` instead, and the next append or subscription tick moves spooled lines into the log in order.
- Logging never fails a protocol change: if even the spool cannot be written, only the line is lost.
- A crash between moving a spooled line and deleting it can repeat that line; a client deduplicates by `id`.

```json
{"id": "<32 hex>", "kind": "mail", "at": 1795675819.4, "crew": "jack", "assignment": "<assignment id>", "message": "<message id>", "state": "sent", "mail_kind": "followup", "question": null, "text": "..."}
```

| kind | Written by | Fields beyond id, at, crew, assignment |
| --- | --- | --- |
| `recruit` | first assignment of a crew incarnation | `task, paths, actions, verify, handoff` |
| `assign` | a handoff to the same incarnation | `task, paths, actions, verify, handoff` |
| `mail` | `crew`, `assign`, `tell`, `answer`, resolve_grant, delivery hooks, `inbox` | `message, state` (`sent`, `read`, `bounced`), plus `mail_kind, question, text` when sent and `reason` when bounced |
| `question` | `ask` | `question, state: "asked", text, request` (grant request id or null) |
| `grant` | `grant`, `ask` after a gate denial, resolve_grant | `state` (`requested`, `granted`, `denied`), `command, request`, plus `tool, cwd, code, category, question` when requested and `reason` when denied |
| `report` | `done` | `report, verification` |
| `lifecycle` | supervisor and `wait` | `state` (`started`, `failed`, `stopped`, `retained`, `pending`, `exited`, `lost`), `detail` |

Lines are capped at 64 KiB; an oversized action drops its largest payload fields and keeps its identity, with `payload_truncated: true`.

## Session hub

The app keeps no session list of its own; it reads captain state, and needs no second long-running process.

- List: `captain sessions`, run in any project with no Herdr and no supervisor, prints the list as one JSON line; a running supervisor's `sessions` method returns the same: `{projects: [{project, exists, sessions: [{id, name, transport, state, last_activity, ended_at, crew}]}]}`, the current project first, then by project, newest first.
  `transport` is `supervisor` or `herdr`; `state` is `running` (a supervisor holds the lock), `ended`, `stopped` or `herdr` (a Herdr session, which the app cannot resume).
- Start: run `captain supervise` in the project; resume: `captain supervise --session ID` there, which clears `ended_at`.
- Rename: `rename`, or `captain --session ID --rename NAME` with no supervisor running.
- End: `shutdown` with `end: true` stops every crew, records `ended_at` in `session.json` and exits; plain `shutdown` (quitting the app) stops without ending.

## Transcripts

`transcript` and `step` read an agent's own native session files, never a copy: the Claude transcript, the Codex rollout and the pi session file, found from the crew's native-history manifest, else from the transcript path or Codex thread its events name.
`agent` is a crew id or `captain`; `incarnation` reads an earlier incarnation of that crew.
Files removed by native-history cleanup after a dismissal are skipped, so that crew's transcript reads as empty.

```json
{"id": "0:18342:2", "at": "2026-11-26T10:32:51.264Z", "role": "assistant", "kind": "tool_call", "text": "{\"command\": \"ls\"}", "tool": "Bash", "call": "toolu_01", "clipped": false}
```

- An entry is one block of one native record: `role` `user`, `assistant`, `tool` or `system`, `kind` `text`, `thinking`, `tool_call` or `tool_result`; `call` links a result to its call.
- `id` is `FILE:OFFSET:BLOCK`: the index into `files`, the record's byte offset and the block; native files only grow, so it stays valid.
- A page's `text` is cut at 4096 characters with `clipped: true`; `step` returns that entry's whole block (up to 1 MiB), for full step output on demand.
- Without `after`, a page is the newest entries before `before` (default: the end); pass `start` back as `before` for older entries and `end` as `after` for newer ones.
  A page holds at least `limit` entries when that many exist and never splits a record; `more` says whether that direction holds further entries.
- Reading backwards rescans the files from their start, so an older page of a very long session costs a full read.

## pi crew

- The crew runs `pi --mode rpc` behind `pi_rpc`, with the same extension and session directory as a pane crew; its gate is that extension, which fails closed.
- A model switch needs a `provider/model` id.

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
- An isolated Claude background crew runs with `setting_sources=[]`, yet claude still loads managed policy and `~/.claude.json`.

## Events

Each finished turn is appended to `events/<crew>-<incarnation>.jsonl` through `captain hook`, in Codex's own notify shape, so `wait`, `status` and usage read it as they read a pane crew's:

```json
{"type": "agent-turn-complete", "thread-id": "<thread>", "turn-id": "<turn>", "cwd": "/path/to/checkout", "input-messages": ["<mail text>"], "last-assistant-message": "<reply>", "provider": "codex", "incarnation_id": "<incarnation>", "seq": 7, "timestamp": "<iso>"}
```

A relayed approval is recorded like any hook decision, as `hook_event_name: "PermissionDecision"` with `approval: {verdict, code}`.
A supervised Claude crew's own hooks write its events, exactly as in a pane.
A supervised crew whose native CLI exits without a report is closed as lost with `[supervised-crew-exited]`.

## Redaction

Every notification, every `command`, `transcript` and `step` reply and every action log line passes one redaction: a value under a key ending, case, dash and underscore aside, in `apikey`, `secret`, `password`, `authorization` or `token` becomes `"[redacted]"`, and the supervisor token is replaced wherever it appears.
Inside strings, a serialized pair such as `"apiKey": "..."` is redacted too, including one cut off at the end of the text.
Counts such as `input_tokens` are kept.
`command` output is redacted before it is clipped, so a secret on the 1 MiB boundary never shows a prefix.
The action log redacts the token from the writer's environment, and in the supervisor its own token.
Native event files are the hooks' own evidence and stay as written; only what leaves the socket is redacted.

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
