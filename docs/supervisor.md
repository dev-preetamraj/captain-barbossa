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
  The supervisor strips `CAPTAIN_SUPERVISOR_TOKEN` from every process it spawns, except the supervised captain, which gets a second, scoped token there (see Supervised captain).
- SIGINT, SIGTERM, SIGHUP or the `shutdown` method stop the captain, then every crew, then any sign-in, remove the socket and exit.
- When stdin is a pipe or socket (an app launcher), its end of file does the same, so a crashed app never leaves an orphan holding the lock; the app keeps stdin open for the supervisor's life, and anything written to it is ignored.
  A terminal or `/dev/null` stdin is never watched.
  A client disconnecting from the socket (a closed app window) changes nothing; crew keep running.
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
| `hello` | none | `{protocol, version, session_id, project, capabilities, methods, notifications, commands, authorized_commands}` |
| `command` | `argv` (list of strings, verb first) | `{exit, stdout, stderr, result?, refusal?, truncated?}`; for `do` and `quiet`, `{authorization}` |
| `resolve_authorization` | `request`, `decision` (`approve` or `deny`), `reason` (optional) | `{request, state, run?}` |
| `authorizations` | none | `{authorizations: [request, ...]}` |
| `questions` | none | `{questions: [{crew, assignment, question, text, grant_request, delivered_at, escalated, waiting_on}]}` |
| `diff` | `assignment` | `{assignment, baseline, current, root, paths, files, diff, truncated}` |
| `mark_reviewed` | `assignment`, `result` (optional), `note` (optional) | `{assignment, result, at, note}` |
| `start_captain` | `provider`, `model` (optional, default `[captain]` settings), `effort` (pi only) | `{provider, transport, model, state: "started"}` |
| `captain_state` | none | `{state, provider?}`; state `idle`, `working`, `blocked` or `null` |
| `message_captain` | `text`, `message_id` (optional, 1 to 128 of `A-Za-z0-9_-`; minted when absent) | `{message_id, delivery_id, state, landed}`; state `delivered`, `queued` or `uncertain` |
| `interrupt_captain` | `reason` (optional) | `{outcome}` |
| `stop_captain` | none | `{outcome}`; `stopped`, `pending` or `not-running` |
| `captain_requests` | none | `{requests: [{id, kind, payload, state, at, answer?, resolved_at?}]}` |
| `answer_captain` | `request`, `decision` (`approve`/`deny`, for an approval) or `answers` ({question id: text}) | `{request, state: "answered"}` |
| `subscribe` | `cursors` (optional object: stream name to `{offset, generation}` or a bare offset) | `{session_id, ends}`, then notifications on the same connection |
| `transcript` | `agent`, `after` or `before` (optional cursors), `limit` (1 to 500, default 200), `incarnation` (optional) | `{agent, provider, files, entries, start, end, more}` |
| `step` | `agent`, `id`, `incarnation` (optional) | one entry with its whole text |
| `rename` | `name` | `{session_id, name}` |
| `sessions` | none | the session list (see Session hub) |
| `providers` | `recheck` (optional bool), `provider` (optional) | `{providers: [{provider, installed, version, signed_in, account, detail, checked_at, sign_in, attempt}]}` |
| `sign_in` | `provider`, `method` (optional), `api_key` (with `apiKey`) | the attempt, or `{provider, state: "guidance", guidance}` |
| `cancel_sign_in` | `provider` | the attempt, or `{provider, state: "not-running"}` |
| `usage` | none | `{agents: [{agent, usage}]}`; `usage` is a frame or null |
| `resolve_captain_delivery` | `delivery_id`, `decision` (`resend` or `ack`) | `{delivery_id, decision}` |
| `uncertain_deliveries` | none | `{deliveries: [{delivery_id, at, delivery}]}`, oldest first |

The CLI calls `start`, `ring`, `stop`, `state`, `interrupt` and `set_model` itself, from `crew`, `tell`, `wait`, `dismiss`, `status` and `model`.
An app calls every other method directly.

- `start` refuses a lifetime that is not the crew's own recorded launch, a crew not marked `"execution": "supervised"`, a provider with no supervised transport, and any start once `shutdown` began.
  It refuses with `[codex-hooks-missing]` when Codex does not load all four captain hooks (SessionStart, Stop, PreToolUse, PermissionRequest; Codex 0.164.0 or newer), and with `[codex-hooks-untrusted]` when any of them is untrusted, modified or disabled.
  The user fixes that once by approving the hooks in a Herdr Codex crew's review screen; Captain never writes Codex trust.
- `interrupt_all` interrupts every running crew in parallel and records `interrupted` in session memory for each confirmed one.
- `ring` with a `message_id` Codex already accepted lands again without starting a second turn.
  A `turn/start` Codex never answered reports `"delivery unconfirmed"`: held, never bounced, and settled by the next turn notice.

Refusal codes by method: `supervisor-params` (any), `supervisor-crew`, `supervisor-transport`, `supervisor-closing`, `supervisor-startup`, `codex-hooks-missing`, `codex-hooks-untrusted`, `claude-sdk-missing`, `claude-start` (start), `grant-request-unknown`, `grant-request-closed`, `grant-request-reason`, `grant-request-decision`, `grant-request-unsupported` (resolve_grant), `command-refused`, `command-timeout`, `command-failed` (command), `transcript-agent`, `transcript-range`, `transcript-entry`, `transcript-file`, `transcript-gone` (transcript, step), `session-name` (rename), `supervisor-captain-token` (any method but the captain CLI's, with the captain's token), `captain-delivery-decision`, `captain-delivery-unknown` (resolve_captain_delivery), `sign-in-method`, `sign-in-running`, `sign-in-missing` (sign_in), `authorization-argv` (command), `authorization-unknown`, `authorization-closed`, `authorization-decision` (resolve_authorization), `diff-assignment`, `diff-no-baseline`, `diff-git` (diff), `review-assignment`, `review-no-report`, `review-result` (mark_reviewed), `captain-transport`, `captain-running`, `captain-start` (start_captain), `captain-not-running` (message, interrupt and answer), `captain-message-id` (message_captain), `captain-request-closed`, `captain-request-answer`, `captain-request-unknown` (answer_captain).

### hello

`protocol` is `captain.supervisor/1`; a client refuses any other major version.
`capabilities` has one row per provider:

```json
{"codex": {"supervised": true, "transport": "app-server", "delivery": "body", "gate": "hooks", "output": "items", "limits": ["Needs Codex 0.164.0 or newer, ..."]}}
```

- `supervised` is whether this supervisor can start that provider's crew; `transport` is null when it cannot.
- `delivery` is `body` (the ring carries the rendered mail) or `hook` (the ring is a wake line and the crew's own hook hands the body over).
- `gate` is `hooks`, `extension` (pi) or `none` (grok: its own auto mode is the whole gate, so it fails open).
- `output` is `items` (Codex app-server item and turn notifications), `events` (pi RPC session events), `messages` (Claude: every Agent SDK message, partial deltas included) or null.
- `limits` states each provider's known gaps in plain words.
- `sign_in` is `{methods, guidance}`: the sign-in methods `sign_in` accepts (none for Claude, pi and Grok) and what the user does otherwise.

### command

`argv[0]` must be one of `crew`, `assign`, `tell`, `answer`, `grant`, `interrupt`, `model`, `wait`, `status`, `dismiss`, `recover`, `memory`, `result`, `worktree`, `dashboard` (only as `dashboard --once`) or `inspect`.
`do` and `quiet` never run from `command` alone: they open an authorization request (see Captain authorization).
The verb runs as `python -m captain_barbossa --session <id> ARGV...` in the project, as the captain: crew identity variables are dropped, `CAPTAIN_SESSION` and the supervisor token are set, stdin is empty.
The CLI stays the only writer of captain state, and the reply is the verb's own exit code and output; a non-zero exit is a result, not a refusal.
The reply is also typed, so a client never parses CLI text: on exit 0, stdout that is JSON comes back parsed as `result`; on any other exit, `refusal` is `{code, message}`, with the protocol code found in the CLI's error (`captain-error` when it names none).
Each verb has its own deadline (`supervisor.COMMANDS`: 30 to 180 seconds, `wait` 600); past it the process group is killed and the call is refused with `[command-timeout]`.
Both pipes are drained as the verb runs and kept to 1 MiB plus a 4 KiB redaction margin each; the rest is read and dropped.
A client sets its own read deadline above the verb's, and passes `wait --timeout` below 600.

### subscribe

Streams are `actions` (the protocol action log) and one per native event file, named by its stem (`captain`, `<crew>-<incarnation>`).
A stream missing from `cursors` replays from 0, so `{}` replays the whole session.
A stream's `generation` names the file itself (device and inode); a cursor whose generation differs, or whose offset is past the file's end, replays that stream from 0 and its first notification carries `reset: true`, so the client drops what it held for the stream.
A bare-offset cursor has no generation and resets only past the end.
The result's `ends` holds each stream's cursor `{offset, generation}` at subscribe time, past its last complete line: a client whose held cursor for every stream has reached its end is caught up, so a read made right after `subscribe` (`questions`, `authorizations`, `captain_requests`) lines up with the stream.
Notifications, all redacted (see below):

| Notification | Params | Replayed |
| --- | --- | --- |
| `event` | `{stream, crew, cursor, generation, reset, events}`: lines past the previous cursor, at most 1 MiB per notification; `crew` is null for `actions` and `captain` | yes, from `{offset: cursor, generation}` |
| `delivery` | one `wait --all` delivery: `{status, delivery_id, crew, assignment_id, summary}` | while unacknowledged |
| `output` | `{crew, method, params}`: a native notification as the provider sent it, or with `method: "captain/usage"` a live usage frame (see Live usage) | no |
| `attention` | `{crew, code, message}`: a start refused (hooks missing or untrusted, SDK missing), a native auth failure, with `code: "captain-delivery-uncertain"` and `delivery_id` an unconfirmed captain delivery, or with `code: "sign-in"` and `sign_in` a sign-in's progress | no |
| `heartbeat` | `{at}` (epoch seconds), sent when nothing else was sent for 10 seconds | no |

- A client keeps the latest `cursor` per stream and passes them to its next `subscribe`, so a reconnect misses no line and repeats none.
- `delivery` is the projection `wait --all` returns, sent once per subscription while its `delivery_id` stays pending; it never acknowledges, so the captain's own `wait` still receives and acks it.
- `output` and `attention` are live only, and a subscriber more than 1000 notifications behind loses the oldest ones rather than stalling a crew.
- The supervisor closes a subscription with no final message when it stops or when one notification write takes longer than `request_timeout`; a client that sees the close, or no line for 30 seconds, reconnects with its cursors.

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
| `question` | `ask`, supervisor | `question, state: "asked", text, request` (grant request id or null); `state: "routed", waiting_on` when its waitingOn changes |
| `grant` | `grant`, `ask` after a gate denial, resolve_grant | `state` (`requested`, `granted`, `denied`), `command, request`, plus `tool, cwd, code, category, question` when requested and `reason` when denied |
| `report` | `done` | `report, verification` |
| `lifecycle` | supervisor and `wait` | `state` (`started`, `failed`, `stopped`, `retained`, `pending`, `exited`, `lost`), `detail` |
| `authorization` | `captain do`, `command`, resolve_authorization (crew is `captain`, assignment null) | `request, state` (`open`, `approved`, `denied`, `spent`), plus `origin, argv, command, category` when opened and `reason` when decided |
| `review` | mark_reviewed | `result` (null for the report), `note` |
| `prompt` | message_captain, the delivery bridge (crew is `captain`, assignment null) | `message, state` (`queued`, `delivered`), plus `text` when queued |

Lines are capped at 64 KiB; an oversized action drops its largest payload fields and keeps its identity, with `payload_truncated: true`.

## Captain authorization

In a supervised session, `captain do` and `quiet` are gated by requests the user resolves in the app, recorded in `protocol.json` under `authorizations`.
A Herdr session is unchanged and reads or writes nothing for this.

```json
{"id": "<32 hex>", "origin": "captain", "argv": null, "command": {"do_command": "push"}, "category": "outward", "state": "open", "at": 1795675819.4}
```

- `category` is `outward` (`push`, `fetch`, `pull`), `destructive` (`run`: a Makefile recipe can do anything) or `local` (`commit`, `branch`, `switch`, `quiet`).
- The captain's own `captain do` of an outward or destructive verb with no approved request opens one (or finds the open one for the same command) and is refused with `[authorization-required]`; it runs once the user approves, on the captain's rerun of the same command.
  A local verb runs as before.
- Through `command`, every `do` and `quiet` argv, local ones too, only opens a request (origin `app`) and returns `{authorization: request}`.
  Approving it with `resolve_authorization` runs it at once as the captain, with `CAPTAIN_AUTHORIZATION` naming the request, and the reply carries the run's typed `command` result as `run`.
- An approval authorizes exactly one run of exactly that command (`command` holds the parsed fields), is spent by it, and expires unspent after 15 minutes; a denial is final.
- Every request and decision is an `authorization` protocol action, so a subscribed app shows the card as soon as the request opens.
- A supervised captain learns the outcome of a request it opened: the delivery bridge sends it "approved: run that exact command again now" or "denied" once, and records `notified_at` on the request.

## Questions: waitingOn

A supervised crew's question waits on the captain first.
It moves to the human, and stays there, once it is escalated (unanswered through its re-ask), or once the captain has received it (acknowledged the `asked` delivery) and then ended a turn without answering it.
The supervisor decides this from the delivery acknowledgement (`delivered_at`, set when `wait` acks the question) and the captain's own turn-end hook events, on each subscription projection and drain tick and on `questions`.
Each change is recorded as the question's `waiting_on` in `protocol.json` and as a `question` action with `state: "routed"`.
Answering is unchanged: `answer`, through `command`.
The captain can route a question to the user itself, for good, with `captain escalate NAME QUESTION_ID` (an app session only; `[escalate-supervised]` elsewhere), recorded as `waiting_on: "human"`, `escalated_by: "captain"` and a `routed` action.

## Review: per-assignment diff and reviewed state

- Once a supervised assignment is validated and reserved, and before its crew gets the task, `protocol.begin` records `baseline`: a Git tree id of every tracked and untracked, non-ignored file as it stands.
  A failure is recorded as `baseline_error` and named by `diff` (`[diff-no-baseline]`); a worktree crew's baseline is taken in its own worktree; a Herdr assignment takes none.
- Snapshots never touch the repository: files are hashed as they stand with `hash-object --no-filters` into the session's own object store (`sessions/<id>/snapshots`, reading the repository's through alternates) and built in a scratch index from `read-tree`, so no object, index (split or not), hook, filter or fsmonitor of the user's is written or run.
- `diff` re-hashes only the assignment's owned and claimed paths over the baseline and returns `git diff --relative baseline current` for them, with literal pathspecs relative to the execution root, so another crew's edits and earlier uncommitted work stay out.
  `files` is the numstat (`added`/`deleted` null for binary); `diff` is cut at 1 MiB with `truncated: true`.
- `mark_reviewed` records `reviewed: {at, note}` on the assignment for its report, or `reviewed_results[result]` for one isolated result of it, and logs a `review` action; the CLI reads both from `protocol.json`.

## Supervised captain

`start_captain` runs the session's captain itself: the provider's first `TRANSPORTS` entry (`codex app-server`, Claude through `claude_host`, `pi --mode rpc`; grok is refused), with `prepare_launch`'s instructions and launch state, the supervised-session instruction text and no pi delivery extension.

- The captain is not a crew: no lifetime record. It runs in its own process group with a scoped token as `CAPTAIN_SUPERVISOR_TOKEN`, so its `captain` commands reach this supervisor. `stop_captain`, `shutdown` and the supervisor's own exit close its input, wait `stop_timeout`, then kill the whole group.
- `captain.json` records `{provider, pane: null, transport: "supervisor", native_session_id}`: the session id the adapter opened (a Codex thread) is merged in as soon as it opens, and the next `start_captain` with the same provider starts from it, so a stop or crash before the first finished turn still resumes; another provider drops it.
- The scoped token reaches only the methods the captain CLI calls (`hello`, `start`, `stop`, `state`, `ring`, `interrupt`, `set_model`); every other method, such as `answer_captain`, `resolve_authorization`, `resolve_grant`, `resolve_captain_delivery` or `command`, is refused with `[supervisor-captain-token]`, so nothing the captain runs can answer the user's own decisions.
- Its adapter link has `captain` set: Codex skips the crew-only four-hook check and starts its thread with the user's own sandbox and approval settings, as a terminal captain does, and Claude's ring carries the text instead of the crew wake line.
- A captain's turn record (pi's turn-complete, Claude's idle notification) goes to its own hook; only a native permission request becomes a captain request.
- The delivery bridge replaces `captain wait` and pi's extension. Each round it first sends the user's queued prompts in order, then reads `wait --all`'s projection, acknowledges `idle` and `working` at once, and rings every other delivery into the captain as one message (the summary cut at 2000 characters).
  A delivery is claimed in `sessions/<id>/captain-deliveries/` before it is first sent, acknowledged with `wait --ack` only after the adapter reports it landed, and retried while the captain is busy or stopped.
  A send the adapter reports `"delivery unconfirmed"` (in this supervisor) or that an earlier supervisor never confirmed stays claimed: it is never resent or acknowledged, and is reported once per supervisor as an `attention` with `code: "captain-delivery-uncertain"` and its `delivery_id`.
  `uncertain_deliveries` lists every such claim, so a client that missed the attention still finds it; `delivery` is the crew delivery projection, or `{delivery_id, message_id}` for a user prompt.
  The user settles it with `resolve_captain_delivery{delivery_id, decision}`: `resend` drops the claim so the next round sends it again, `ack` marks it delivered so the next round acknowledges it (`[captain-delivery-decision]`, `[captain-delivery-unknown]`); it runs between bridge rounds, never during one.
- `message_captain` queues the user's prompt under `captain_messages` in `protocol.json`, records a `prompt` action, and sends what the captain will take at once; a busy captain gets it from the bridge when its turn ends, and a stopped one from the next `start_captain`.
  Its claim id is `user-<message_id>`, and the ring carries that id, so Codex never starts a second turn for it.
  `message_id` makes a retry safe: a known id returns that prompt's state and sends nothing again, and a known id with other text is refused with `[captain-message-id]`; ids stay known for the newest 200 delivered prompts.
  `state` is `delivered`, `queued` (waiting for the captain), or `uncertain` (settle it with `resolve_captain_delivery`).
- Operator notes: the user's `tell`, `answer`, `interrupt`, `model` and `dismiss` through `command`, `interrupt_all` and `resolve_grant` are recorded under `operator_notes` in `protocol.json` and put, one line each, in front of whatever next starts a captain turn (a prompt, a delivery or an authorization outcome), then marked delivered.
- `interrupt_all` interrupts the captain too, as `{crew: "captain", outcome}`.
- The captain's native prompts go to the user: a Codex approval request or ask-user (`requestUserInput`), a Claude permission request or `AskUserQuestion` (the captain's host alone registers the SDK's `can_use_tool`), and a pi extension dialog (`confirm` as an approval; `select`, `input` and `editor` as a question with the one id `value`) each open a `captain_requests` entry and a `question` action (crew `captain`, `waiting_on: "human"`), an `attention` notification with `[captain-request]`, and wait up to 30 minutes for `answer_captain`; unanswered, it expires and is declined (pi: cancelled).
  Claude's `AskUserQuestion` questions use their own text as the id, and a pi `select` answer must be one of its options.
  `start_captain` expires any request still open from an earlier captain, `stop_captain` expires and declines the ones its captain waits on, and only the newest 200 closed requests are kept.
- Supervised-session instructions swap the Herdr-only lines (panes, tabs, focus, placement, background crew, wait) for app ones and never grow the text; crew recruited in the session get the same swap. Herdr text is unchanged.

## Provider status and sign-in

- `providers` probes each provider once and caches the result until `recheck: true`; probes run in parallel and one that outlasts twice `startup_timeout` reads as unknown with `[provider-check]`.
  `installed` is whether the CLI is on PATH, `version` the first line of its `--version`, and `signed_in` true, false or null (unknown).
  Codex reads `account/read` from a short-lived bare `codex app-server`; Claude runs `claude auth status --json` (Captain never reads, stores or proxies Claude credentials, and keeps only the auth method and email); pi is signed in when `pi --list-models` lists a model; Grok, with no supervised transport, reports only `installed` and `version`.
- `sign_in` for Codex runs `account/login/start` on a bare `codex app-server`: `chatgptDeviceCode` (the default) or `apiKey` with `api_key`.
  The key goes straight to Codex, which stores it itself; the supervisor never writes or logs it and redacts it from every reply and notification.
- Claude, pi and Grok return their `guidance` and start nothing.
- An attempt is `{provider, method, state, user_code, verification_url, detail, output}`; `state` is `starting`, `waiting` (a device code is shown), `signed-in`, `failed` or `cancelled`.
  `sign_in` answers once the attempt leaves `starting` (or after `startup_timeout`), a device code expires after 15 minutes, and a second attempt for the same provider is refused while one runs.
  Every state change is an `attention` with `code: "sign-in"`; a successful one rechecks that provider before it is announced.
- `cancel_sign_in`, `shutdown` and the supervisor's own exit cancel attempts and reap their processes.

## Live usage

Each supervised agent's own protocol reports its usage as a frame, the shape the dashboard reads: `input, output, cache_read, cache_write, tokens, context, limit, model, cost, rate`, with null for anything unknown, never zero.

- Codex: `thread/tokenUsage/updated` (totals, the last call's context and `modelContextWindow`; no cost).
- Claude: each SDK `ResultMessage` (that query's usage, summed; `total_cost_usd` as claude reports it for the session; the context window from `modelUsage` when present; no context).
- pi: `get_session_stats` after each settled turn (totals, cost and `contextUsage`).

The supervisor keeps the newest frame in `events/<stem>.live-usage.json` and publishes it as `output` with `method: "captain/usage"`.
`usage` returns, for the captain and every current crew, the native reading (transcript or rollout) with the live frame filling only what it leaves null; the dashboard of a supervised session shows the same, so a pi crew gets a CTX column.
A Herdr session never reads a live frame.

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
{"id": "0:18342:2", "at": "2026-11-26T10:32:51.264Z", "role": "assistant", "kind": "tool_call", "text": "{\"command\": \"ls\"}", "tool": "Bash", "call": "toolu_01", "origin": null, "clipped": false}
```

- An entry is one block of one native record: `role` `user`, `assistant`, `tool` or `system`, `kind` `text`, `thinking`, `tool_call` or `tool_result`; `call` links a result to its call.
- In the captain's transcript, a `user` text block is split into one entry per part, each with `origin`: `operator-note` (the notes in front), then `user` (a `message_captain` prompt), `delivery` (a crew delivery), `authorization` (an authorization outcome) or `native` (the provider's own context).
  A prompt is known by a digest kept in `sessions/<id>/captain-prompts`; every other entry has `origin: null`.
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
- A crew's host registers no `can_use_tool` callback: the SDK races it against the PermissionRequest hook and the callback always wins.
  claude's own command hooks gate every call, and a call no hook answers is denied by claude itself.
  Only a supervised captain's host registers one, which asks the supervisor (`permission`) and so the user.
- The host streams partial messages and forwards every SDK message as a `message` notification (`kind` names its class, `data` its fields) and each result's usage as `result`, so they reach the app as `output`; one too large for a line is dropped, and `transcript` keeps it whole.
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
Inside strings, a serialized secret-named key's value is redacted too, whatever its JSON type (string, number, boolean, null, array or object), including one cut off at the end of the text.
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
