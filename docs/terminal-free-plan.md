# Terminal-free app: implementation plan

Written on 2026-11-26 by a read-only crew against `df36799`; no code has been written.
Line numbers are against df36799; `~` means approximate.

## 1. Target architecture

```
Desktop client ──JSON-RPC 2.0 over a same-user unix socket + notifications──┐
captain CLI (unchanged verbs) ──supervision.request()───────────────────────┤
                                                                            ▼
              captain supervise  (one per session; holds supervisor.lock flock)
                ├─ ADAPTERS table: codex_app_server | claude_agent | pi_rpc | grok_acp
                ├─ per crew: lifetime.claim → ManagedJob.start(detached, pipes)   (reused)
                │      codex app-server | python -m claude_host (Agent SDK) | pi --mode rpc | grok agent stdio
                ├─ hook relay: provider requests → `captain hook <events path>` (events.py unchanged)
                └─ fan-out: tails events/*.jsonl + protocol.json (eventlog, protocol.wait_all)
State on disk stays the only authority: protocol.json, mail/, events/, lifetimes/, graph.json.
```

- **The seam** is a third value of the roster record's `execution` field.
  `background` already short-circuits Herdr; the new value is `"supervised"`, set only inside sessions whose `session.json` has `"transport": "supervisor"`.
- Every call that touches Herdr for a live crew reduces to five verbs: ring, live status, interrupt, retier and stop.
  For supervised crew these go through one new door, `supervision.py`.
  It is the counterpart of `runtime.herdr` (CODING_STANDARDS S-3: one door per external system, patched at the door).
- I did not add a polymorphic Pane interface, because S-3 and P-2 forbid wrapper or strategy seams.
  The call sites branch the same way they already do for `is_background`.
- **Protocol logic is not duplicated.**
  `protocol.ring` keeps its stamp, landed-body and bounce bookkeeping, and only the "type into pane" step changes.
- The adapters turn transport events into lines in the existing event files, written via `captain hook`.
  That means `poll`, `wait`, `status`, `done`, grants and usage all read the same evidence they read today.

## 2. Phased plan

### Phase 1a: supervisor core plus one Codex crew via app-server, with no Herdr

**New files**

- `jsonrpc.py` (tier 2, stdlib and runtime only): line-delimited JSON-RPC framing.
  - Request-id map, bounded line length, and a monotonic deadline per request (B-1).
  - Envelope and nested-type validation that raises `CaptainError` with the raw line (B-2).
  - One module owns framing; the socket door, the adapters and claude_host all use it.
- `supervision.py` (tier 2, beside `pane`): the CLI's door to the supervisor.
  - Core: `request(directory, method, params, timeout)`, `SUPERVISOR_ERRORS`, `alive(directory)` (a flock probe, never a PID), `socket_path(directory)`.
  - Verbs:
    - `start(crew, lifetime_ref)`
    - `ring(crew, message_id, text) -> {"landed": bool, "gate": str|None}`
    - `interrupt(crew, reason) -> "interrupted"|"idle"`
    - `set_model(crew, model) -> {"model", "applies": "now"|"next-turn"}`
    - `state(crew) -> "idle"|"working"|"blocked"|None`
    - `stop(crew, reason)`
  - The gate strings reuse the existing vocabulary: `"agent not idle"`, `"approval prompt"`, `"awaiting registration"`.
    That is why the bookkeeping in `ring` does not change.
- `supervisor.py` (tier 4, imported only by `cli`):
  - A `ThreadingUnixStreamServer` and a method table.
  - A per-crew `Link` dataclass holding the thread id, active turn and pipes.
    A class is justified here because several functions share that state (P-2).
  - A per-crew owner thread that runs `lifetime.claim` → `job.start(detached=True, stdin=PIPE, stdout=PIPE, stderr=PIPE)` and polls for revocation, exactly like `lifetime.run` (lifetime.py ~770).
  - A hook relay: run `instructions.hook_command(events)` as a subprocess with a timeout, JSON on stdin, output validated.
    Running it as a subprocess keeps the per-process assumptions in `events.py` (fresh process, local imports, cached config).
  - On every turn completion, plus a 30 s tick, it calls `protocol.drain(crew)`.
    This replaces `pump_mail`.
- `codex_app_server.py`: the Codex adapter.
  - `initialize`, then `thread/start` with cwd = execution root, `sandbox: workspace-write`, `approvalPolicy: on-request` (the same as today's flags), and writable roots for `add_dirs`.
  - Delivery: `turn/start` with the rendered body.
    Interrupt: `turn/interrupt` (thread and turn id).
    Retier: a `model` override on the next `turn/start`.
  - The `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` requests become a PermissionRequest-shaped event (`tool_name` Bash or apply_patch, `tool_input.command`, `cwd`, `session_id`, `turn_id`).
    That goes through the hook relay.
    An allow becomes `accept`; anything else becomes `decline`.
    `acceptForSession` is never sent, so grants stay one-shot.
  - `item/tool/requestUserInput` is declined with "use `captain ask`".
  - `turn/completed` is appended in exactly the existing notify shape: `type: agent-turn-complete`, `thread-id`, `turn-id`, `input-messages`, `last-assistant-message`.
    So `crew.event_status`, `protocol.hooks_live` and the thread lookup in `usage` work unchanged.

**Changes to existing files**

- `models.py`: add a `TRANSPORTS` table (S-2).
  This phase adds only the `codex: ("app-server",)` row.
- `instructions.py:272` `native_args`: add a defaulted `transport="tui"` keyword.
  With `"app-server"` it drops `notify` and the TUI-only `--sandbox`, `--ask-for-approval` and `--add-dir` flags, and returns them as thread params.
  The `-c` hook declarations stay identical, so Codex hook trust still applies.
- `crew.py:79`: add an `is_supervised` property.
  In `current_state` (crew.py:110), add a branch that reads events plus `supervision.state` and never reads a pane.
- `protocol.py`:
  - `ring` (802): when supervised, replace the `crew.pane.nudge_block`/`nudge` block with `supervision.ring`.
    Supervisor errors bounce the mail as an unreachable crew does today.
  - `poll` (1181/1215): treat supervised crew as paneless for modal, startup-screen and prompt reads.
    Take the live status from `supervision.state` instead of `herdr_status` (1159).
  - Add `reconcile_supervised`, alongside `reconcile_background` (~1441): it closes an assignment as `lost` when `supervision.alive` is false and `retirement.gone` holds.
  - `drain` (~1003) is unchanged.
- `agents.py`:
  - Refactor first, as its own commit: extract the env, instructions, events-file and `native_args` block of `create_crew` (~826–900) into `crew_launch(...)`.
    Pane crew keep byte-identical output.
  - Add `launch_supervised`, modelled on `launch_background` (598): roster first, then `begin_recruitment`, `native_history.prepare`, `lifetime.prepare`, `deliver(queue_only)`, `supervision.start`, `ring`.
    On failure, `close_unreported("failed")`.
  - In `create_crew` (711/752/818), branch on session transport and refuse placement flags.
  - `interrupt_crew` (164): call `supervision.interrupt`, write memory only on confirmation, and keep the existing ring of held mail.
  - `switch_model` (291): `resolve_model` → `supervision.set_model` → the same `crew_meta`/memory writes.
    The output says when the switch applies.
  - `focus_crew` (149): refuse with "focus is app-owned".
  - `dismiss_crew` (395): add a path with no pane, no Placement or evening, no tab rename and no relayout.
  - `status_crew`: show "supervised" in the PANE column.
- `retirement.py` `retire` (~79): if the crew is supervised, call `supervision.stop` with a bounded wait (the same pattern as `background.cancel`) before `stop()`.
  `stop()` already takes the revoke+check path when the lifetime has no pane (retirement.py ~137).
- `cli.py`:
  - Add a `supervise [--session ID]` subcommand.
    It creates or opens the session with pane `None`, sets `transport` under `crew_meta`, refuses a session that has a workspace, runs the server, and prints one readiness JSON line.
  - `paneless_target` (466) returns true for every command in a supervisor session.
  - Skip `pump_mail` (588) there.
- `defaults.toml`: add a `[supervisor]` table with `request_timeout`, `startup_timeout`, `stop_timeout` and `socket_dir`.

**Reused unchanged:** `lifetime` (claim, start, shutdown, finish, revoke, check), `protocol.begin/deliver/grant/change/wait/wait_all`, `events.append_event`/`permission_decision`/`consume_grant`, `store`, `eventlog`, `memory`, `worktrees` and `results`.

**Tests.** Every test patches `runtime.herdr` to raise.
The fakes are helper scripts in the style of `tests/home_isolation.py`, run under the real lifetime guard.

- `tests/fake_codex_app_server.py`: a stdio JSON-RPC fake that records every request.
- `test_supervised_crew_needs_no_herdr.py` covers the full round trip:
  - `crew` → `thread/start` plus a `turn/start` carrying the rendered body.
  - `tell` mid-turn → held as "agent not idle", then started at `turn/completed`.
  - `interrupt` → `turn/interrupt` with the live turn id.
  - `model` → the next `turn/start` carries the model and `session.json` is updated.
  - A flagged approval → `decline`.
    After `captain grant` it becomes `accept` and the grant list is empty.
  - The crew runs `captain done` (already paneless as a crew verb) → `wait` returns `done`.
  - `dismiss` → the lifetime receipt is `exited`.
- `test_supervisor_socket_door.py`: malformed envelope, timeout, a dead supervisor (all raise `CaptainError`), and a socket path under 104 bytes.
- `test_crew_launch_extraction.py`: pins a foreground recruit's launcher script and lifetime record byte-for-byte across the refactor.
- An addition to the existing paneless-target tests: a Herdr session still resolves a pane.

**Why the Herdr CLI flow is unaffected**

- Every new branch keys on `execution == "supervised"` or `transport == "supervisor"`.
  Only `captain supervise` writes those values, and Herdr sessions always have a workspace id.
- `native_args` defaults to the old output, pinned by the existing argv and instruction tests.
- `captain hook` is untouched.
- The base install gains no dependency, and the full existing suite must pass unmodified.

### Phase 1b: one Claude crew via the Python Agent SDK

**New files**

- `claude_host.py`: runs as `python -m captain_barbossa.claude_host <params>`.
  That argv is recorded by `lifetime.prepare`, so the host and its `claude` child live in the guard's process group.
  - It imports `claude_agent_sdk` lazily.
    If the package is missing it raises `[claude-sdk-missing] install captain-barbossa[app]`.
  - It runs one `ClaudeSDKClient` and speaks `jsonrpc.py` on stdio.
    Requests: `query{text}`, `interrupt`, `set_model{model}`, `close`.
    Notifications: `ready{session_id}`, `turn{working|idle}`, `result{usage,total_cost_usd,model}`, `attention{code}`.
  - `ClaudeAgentOptions`:
    - `cli_path=runtime.executable("claude")`: the user's own install, never the CLI bundled in the SDK wheel.
    - `system_prompt={"type":"preset","preset":"claude_code","append":instructions}`: needed so the SDK does not blank Claude Code's default system prompt.
    - `extra_args` carry the same `--settings` JSON (hooks, no attribution, additional dirs), `--permission-mode auto` and `--session-id` that `native_args` and `native_history.prepare` give a pane crew.
    - `setting_sources=["user","project","local"]`, matching today's foreground crew.
    - `cwd` is the execution root; `env` is the `CAPTAIN_*` identity.
    - No `can_use_tool`: it races the PermissionRequest hook and a fast deny always wins (section 5, item 4).
      The total PermissionRequest hook plus claude's default deny is the backstop, and AskUserQuestion is absent without it.
  - The host exits with claude's return code only after the SDK transport has closed.
- `claude_agent.py`: the supervisor-side adapter for the host, with a row in `TRANSPORTS` and `ADAPTERS`.

**Behaviour**

- Claude stays in `HOOK_DELIVERED`.
  `ring` sends `pane.WAKE_LINE` as a `query`, and the body still arrives through the unchanged `UserPromptSubmit`/`Stop` command hooks via `events.deliver_mail`.
  Stop still blocks an unfinished assignment.
- Retier uses `client.set_model`.
  No picker is scraped and nothing is written to the user's settings, which is an improvement over pane.py:~640.
- Interrupt uses `client.interrupt()`.
- Usage needs no change: the hooks still name `transcript_path`, which `usage._claude_usage` reads.

**Auth**

- The host never reads `~/.claude*` credentials.
- If the CLI reports an auth error, the supervisor posts `protocol.once(crew, "claude_auth", "[claude-auth] run `claude auth login`, or set ANTHROPIC_API_KEY for the supervisor")`.
- `lifetime.prepare` already rejects env keys that look like secrets, so an API key reaches claude only through the supervisor's inherited environment and is never stored.

**Tests**

- `test_claude_host_options.py`: inject a fake `claude_agent_sdk` into `sys.modules` and assert the options above.
  Also assert that `--settings` equals the `native_args` JSON, and that no file appears under the fake `HOME/.claude`.
- `test_supervised_crew_needs_no_herdr.py` gains a Claude case using `tests/fake_claude_host.py`:
  - Assign: wake line plus a body delivered by the real `captain hook UserPromptSubmit` path, with a receipt stamped.
  - Tell, interrupt and set_model.
  - Grant: a real `captain hook PreToolUse` deny, then allow once after the grant.
  - Done.
- `test_supervised_retier_keeps_user_settings.py`.

Unaffected for the same reason as Phase 1a.
Without the extra installed, nothing imports the SDK.

**Phase 1 exit.** An opt-in manual smoke run against real `codex app-server` and real `claude` with the user's own logins, outside CI (TS-1).
It demonstrates assign, tell, interrupt, retier, grant and report end to end.

### Phase 2: client surface (JSON-RPC and event stream)

- In `supervisor.py`, add:
  - `hello` → version `captain.supervisor/1`, session id, and per-provider capabilities.
    The capabilities come from a new `models.CAPABILITIES` table, which states the limits plainly; for example, Grok's hook fails open.
  - `command{argv}` runs a whitelisted captain verb as a subprocess with `CAPTAIN_SESSION` set and a per-verb timeout, and returns `{exit, stdout, stderr}`.
    Initial whitelist: crew, assign, tell, answer, grant, interrupt, model, wait, status, dismiss, recover, memory, result, worktree, `dashboard --once`, inspect.
    `do` waits until the authorization UX exists.
    The CLI stays the only writer.
  - `subscribe{cursors}` sends notifications:
    - `event`: from `eventlog.read_events` by byte offset, so a reconnect replays.
    - `delivery`: a `protocol.wait_all(current, 0)` projection that never acks.
    - `output`: ephemeral assistant and tool deltas.
    - `attention`: auth or hook-trust problems.
- Add `docs/supervisor.md` with the method and notification schema.
- Tests:
  - `test_supervisor_stream_replays_from_cursor.py`
  - `test_supervisor_command_whitelist.py`
  - `test_supervisor_redacts_secrets.py`: `apiKey` params never appear in logs or notifications.
- Unaffected because these are new methods only.

### Phase 3: the captain itself inside the supervisor

- `launch.py:237` `launch_plan`:
  - Drop the "Only pi RPC" refusal (around launch.py:242).
    Each provider's transport (`rpc`, `app-server`, `sdk`, `acp`) comes from `TRANSPORTS`, and the JSON stays `version: 1` with the `transport` field.
    `prepare_launch` (launch.py:~145) is reused as-is.
  - For terminal launches, `launch()` is unchanged.
- The supervisor starts and owns the captain directly.
  `lifetime` is crew-only and refuses `crew_id == "captain"`.
  Shutdown is bounded and kills the process group, as in `processes.py`.
- Delivery bridge: port the ingestion-before-ack logic of `pi_captain.py` to Python.
  It runs `wait_all`, steers or prompts the summaries into the captain through its adapter, and acks only after the adapter confirms acceptance.
- `instructions.agent_instructions`: in supervised sessions, swap the Herdr-specific lines ("inside Herdr", focus, relayout, dashboard pane) for replacements without growing the text, so `tests/test_instruction_size.py` stays at its limit (TS-4).
- Tests: `test_launch_plan_transports.py` and `test_supervised_captain_acks_only_ingested.py`.
- Unaffected because the terminal launch path is untouched and the new plan fields are additive.

### Phase 4: pi RPC crew (4a) and Grok ACP crew (4b)

- **`pi_rpc.py` (4a)**
  - Launches `pi --mode rpc` with the existing `pi_history` extension, whose gate fails closed.
  - Body-on-ring delivery uses `prompt` when the crew is idle.
    `abort` interrupts; `set_model` retiers; `get_state` reports liveness.
  - `follow_up` and `steer` stay unused unless you choose mid-turn steering (Q8).
- **`grok_acp.py` (4b)**
  - Launches `grok agent stdio` and drives it with `session/new`, `session/prompt`, `session/cancel` and `session/set_model`.
  - `session/request_permission` is answered through the hook relay, so a gate that fails closed on our side covers every call Grok asks about.
  - Pin and verify a Grok version floor in `models.py`.
  - Usage: add a supervisor-written usage ledger next to the events file, which `usage._live_usage` reads for grok (it returns `None` today, usage.py ~141).
    The ledger keeps accumulating across resumes, because Grok's own totals reset on resume.
- Tests: fake stdio peers, the same round-trip file, and `test_grok_usage_survives_resume.py`.

### Phase 5: isolated Claude background crew

- `background.py:46` `SUPPORTED`: add `claude` only when the SDK extra imports.
- `_execute` (background.py ~285) runs `claude_host` in one-shot mode with:
  - `setting_sources=[]`
  - read-only `allowed_tools` (Read, Grep, Glob)
  - `can_use_tool` denying everything
  - `--strict-mcp-config` and no MCP servers
  - `--settings` containing only Captain's PreToolUse hook
- `models.headless_report` gains a Claude result parser.
- Disclose in docs that managed policy and `~/.claude.json` still load.
- Tests: option assertions, and the refusal is unchanged when the extra is missing.

### Phase 6: in-app auth and usage

- Codex: pass `account/read` and `account/login/start` (chatgptDeviceCode or apiKey) straight through to app-server.
  The key goes to Codex's own store and is never stored or logged by the supervisor.
- Grok: run `grok login --device-auth` as a supervised subprocess, with the device code shown in the app.
- pi: guidance for an API key or `/login`.
- Live CTX for the dashboard from `thread/tokenUsage/updated`, SDK `ResultMessage` and pi `get_session_stats`.
  Unknown values stay unknown.

**Every phase** updates AGENTS.md (which currently says "no daemon"), docs/plan.md and the README, and bumps `pyproject.toml` before any push to `main`.

## 3. Dependencies

- **Supervisor core, Codex, pi and Grok:** none.
  Only the standard library (`socketserver`, `selectors`, `threading`, `json`) plus the user's installed CLIs.
- **`claude-agent-sdk` (Python):** should be an **optional extra**, `captain-barbossa[app]`.
  - It pulls in anyio and mcp, and ships a platform-specific bundled Claude Code binary that we deliberately do not use.
  - The base CLI stays `questionary`-only.
  - Tests use a fake module, so it does not need to be in the dev group.
    An optional contract test could pin a minimum SDK version.
  - Check that `uv tool upgrade` (`captain update`) keeps the installed extra.
- **Desktop client (Tauri 2 + React, or SwiftUI):** a separate repo with no Python dependency.

## 4. Risks and open questions for you

1. **Daemon policy.** AGENTS.md and plan.md say "no daemon".
   B-1 exempts only interactive sessions owned by `lifetime` from having a total deadline, and explicitly excludes background work.
   Do you approve extending that exemption to supervised crew turns?
   Every RPC request would still be bounded.
2. **Socket path on macOS.** `sun_path` is capped at 104 bytes, and the state-root path is about 165.
   The proposal is a short private directory (`/tmp/captain-<uid>/s-<id12>.sock`, 0700, ownership checked), with a pointer stored in the session directory.
3. **Same-user socket.** A crew process could connect and run captain-only verbs.
   Today's crew guard is environment-based, so this is consistent with "guard-rail, not a security boundary".
   Do you want a per-launch token passed only to the captain's environment?
   Crew environments are built explicitly and never receive it.
4. **Claude under the SDK (to verify in the 1b spike):**
   - Do `--settings` command hooks fire when passed through `extra_args`, including when `setting_sources=[]`?
     Flag settings are a separate source from user, project and local.
   - Does the Python SDK accept `--permission-mode auto`?
   - Does a host that is not the native process weaken native-history cleanup?
     That cleanup trusts `native_returncode`.
5. **Codex app-server (to verify against 0.172.0 with `codex app-server generate-json-schema`):**
   - Are `-c` hook and config overrides honoured?
   - Does `notify` double-fire?
   - What are the exact `sandbox`, `approvalPolicy` and writable-roots field names?
   - Hook trust still has to be reviewed in Codex itself and cannot be done in-app.
     Should we use `approvalPolicy: "untrusted"` so approval requests become a complete per-call gate even without trusted hooks?
6. **Grok mode.** Should Grok crew drop `--permission-mode auto` so ACP permission requests reach the gate?
   That is stricter, but noisier.
7. **Supervisor crash.** The lifeline kills every crew.
   That matches panes closing on captain exit, but resuming via `thread/resume` or SDK `resume` is out of scope until you want it.
8. **Mid-turn mail.** Keep today's rule that mail lands at the turn boundary, or let `tell --steer` use `turn/steer`, pi `steer` or SDK streaming input?
9. **Native ask-user requests.** Deny them with "use `captain ask`" (proposed), or map them to protocol questions?
10. **Claude login reuse in a distributed app.** You chose the T3 model.
    The research still flags Anthropic's third-party policy as unverified, so I recommend asking Anthropic before shipping.
11. **Mixed sessions.** Should a Herdr session be allowed to recruit supervised crew, to get structured interrupt and retier there?
    It is out of scope here; say if you want it.

## 5. Decisions on the open questions (2026-11-26)

Constraint: the feature ships in a normal new release and must not change the Herdr CLI workflow for live users.
New code paths are reachable only from a session that `captain supervise` created.
Additive surfaces are allowed: the `supervise` subcommand, a `[supervisor]` settings table and the optional `[app]` extra.
Phase 1a starts with a golden test that pins, for Herdr sessions, launcher argv, hook command strings and generated instructions byte-identical to `df36799`.
The existing suite passes unmodified.

1. Supervised turns have no total deadline; every RPC request stays bounded.
   `captain supervise` is a long-running process, approved as an exception to "no daemon", and AGENTS.md says so from Phase 1a.
2. The socket lives at `~/.captain/run/s-<id12>.sock` and is refused at 104 bytes or more.
   It is never under `/tmp` or the OS temp dir, which are crew scratch.
3. A per-launch token is required from Phase 1a.
   The supervisor hands it to the app on the readiness line and never writes it to disk.
4. The SDK spike gates Phase 1b.
   It also checks whether `can_use_tool` pre-empts the PermissionRequest hook.
   Spike (claude 2.1.332, SDK 0.2.189): `extra_args` `--settings` hooks fire with any setting sources, and `--permission-mode auto` is accepted; `can_use_tool` races the PermissionRequest hook and a fast deny always wins, so the host registers none and claude denies calls no hook answers.
   An interrupted turn runs no Stop hook, so the host reports the idle itself; `interrupt`, `set_model`, `--session-id` and `transcript_path` work as native_history and usage expect.
   The SDK drops its process handle on disconnect, so the host keeps it first and counts claude's 143 after its own close as a clean exit.
5. Codex keeps `approvalPolicy: on-request`.
   `untrusted` is rejected because the gate returns DEFER for unclassified commands, and DEFER denies.
6. Grok permission mode is decided in the Phase 4b spike.
   Relaying Grok permission requests needs grok in `events.GATED_PROVIDERS`.
7. A supervisor crash kills its crew, and the next CLI call closes their assignments as `lost`.
   Resume is out of scope.
8. Mail lands at the turn boundary; steering comes later.
9. Crew deny native ask-user requests with "use `captain ask`".
   The Phase 3 supervised captain surfaces them to the app instead.
10. Settled by Decision 4 in `terminal-free-app.md`.
11. Out of scope and enforced: `create_crew` refuses supervised crew in a Herdr session.
