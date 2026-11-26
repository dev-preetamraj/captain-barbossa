# Terminal-free app

Goal: let a desktop app run captain and crew without a terminal or Herdr.
The existing CLI + Herdr flow keeps working unchanged; the new path is additive.

Status (2026-11-26): Phases 1-6 built and reviewed.
Phase 1 (supervised Codex and Claude crew) was live-tested against the real CLIs; Phases 2-6 were tested against fake peers and reviewed.
Still needs live checks:

- A real Grok: the adapter, the 1.0.62 version floor and the switch to an asking mode were built against a fake ACP peer, and Grok is not installed on this machine.
- Grok device auth: `grok login --device-auth` output parsing and Grok's signed-in status, which has no verified status command.
- Claude cost fields: `total_cost_usd` and `modelUsage` from a real `ResultMessage`.
- Live captain turns: the supervised captain on each provider, its delivery bridge and its approval and ask-user prompts.

Not built: the model catalog with effort levels and the settings methods (see `terminal-free-ui-gaps.md`).
Branch: `feat/terminal-free-app`.
Baseline: `main` at `df36799` (0.51.0).

## Decisions

1. **Supervisor.** A pane-free supervisor owns the provider processes and exposes JSON-RPC plus an event stream to a thin client.
2. **Adapters.** Codex app-server, Claude Agent SDK (Python), pi RPC, Grok ACP.
3. **Authority.** The captain protocol stays the source of truth for assignments, grants, mail, receipts, done, memory, and worktree results.
4. **Claude auth follows T3 Code.** Drive the user's installed, unmodified Claude Code through the Agent SDK with their own `claude auth login`.
   Never read, store, or proxy Claude tokens.
   Offer an API key as the alternative.
   Anthropic's legal page allows this (a user signing in to the unmodified binary), so the plan's open question 10 is settled.
5. **Desktop client.** Electron + Bun with a React/TypeScript UI, in the separate `barbossa` repo (`/Users/preetam/code/neufin/barbossa/app`).
   It talks to the supervisor's JSON-RPC and event stream, and uses a mock client until that exists.
6. **Live CLI unchanged.** The feature ships in a normal release, and the Herdr CLI workflow behaves exactly as in `df36799`.
   New code runs only in supervisor sessions, and a golden test pins the Herdr path byte-for-byte.

## Login per provider

| Provider | Path | Notes |
|---|---|---|
| Codex | User's existing `codex login` | app-server also has `account/login/start` (`chatgptDeviceCode`, `apiKey`, `chatgptAuthTokens`) |
| Claude | User's own `claude auth login`, or `ANTHROPIC_API_KEY` | Anthropic allows a user signing in to the unmodified binary; offering Claude.ai login ourselves or handling tokens is not allowed |
| pi | User's own pi auth, or a provider API key | pi `/login` needs its terminal UI |
| Grok | `grok login --device-auth`, or `XAI_API_KEY` | Device code can be shown in-app |

## Verified provider facts

Checked against primary docs on 2026-11-26.

| Provider | Facts |
|---|---|
| Claude Python SDK | `set_model`, `interrupt`, `can_use_tool`, `AskUserQuestion`, `PreToolUse` hooks. `setting_sources=[]` disables user, project, and local settings, hooks, CLAUDE.md, and skills; managed policy and `~/.claude.json` still load. Omitted, it loads everything like the CLI. CLI `--bare` skips hooks. |
| Codex app-server | `turn/start`, `turn/steer`, `turn/interrupt`, `thread/resume`, `item/tool/requestUserInput`, `hooks/list`, `thread/tokenUsage/updated`. Latest 0.172.0; this machine has 0.163.0. |
| pi RPC | `prompt`, `follow_up`, `steer`, `abort`, `set_model`, `get_session_stats`, `get_state`. Latest 1.6.1; 1.6.0 installed. |
| Grok ACP | `grok agent stdio`; `session/new`, `session/prompt`, `session/cancel`, `session/set_model`. `PreToolUse` is the only blocking hook and failures are fail-open. Usage totals reset on resume. Not installed on this machine. |

Isolated Claude background crew are now possible through `setting_sources=[]`.
Grok hooks cannot be trusted as the grant gate, so captain's own pre-tool gate stays.

## Reuse before writing new code

- `launch-plan` JSON (`launch.py:237`) and the paneless background runner (`background.py`).
- `protocol.py`, `store.py`, `eventlog.py`, `memory.py`, `usage.py`, `lifetime.py`.
- Put an interface at the Herdr call sites (`runtime.py`, `pane.py`, `crew.py`) instead of duplicating protocol logic.

## Semantics that must survive

- A provider finishing a turn is not `captain done`.
- Grants stay exact-command, one-shot, and assignment-scoped.
- Mail is recorded before delivery, and the receipt waits for the hook flush.
- Unknown cost stays unknown, not zero.
- Background crew stay restricted read-only jobs.

## First milestone

One Codex crew via app-server and one Claude crew via the SDK, driven by the supervisor with no Herdr.
It proves assign, tell, interrupt, retier, grant, and report end to end.
Existing tests keep passing.

## Working rules

- Branch first; agree the plan before any edits.
- No commit or version bump unless asked.

## References

- Research: `/Users/preetam/code/neufin/barbossa/docs/macos-app-research.md` (skip the Discord section).
- Implementation plan: [terminal-free-plan.md](terminal-free-plan.md).
- Anthropic auth terms: https://code.claude.com/docs/en/legal-and-compliance
- Claude SDK settings isolation: https://code.claude.com/docs/en/agent-sdk/claude-code-features
- Codex app-server: https://developers.openai.com/codex/app-server
- pi RPC: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md
- Grok headless and hooks: https://docs.x.ai/build/cli/headless-scripting, https://docs.x.ai/build/features/hooks
- T3 Claude provider: https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-claude.md
