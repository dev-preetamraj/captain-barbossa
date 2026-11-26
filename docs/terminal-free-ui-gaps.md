# Terminal-free app: gaps the UI needs closed

The desktop app's UI definition (`docs/ui.md` in the `barbossa` repo, section 11) found needs that `docs/terminal-free-plan.md` does not yet meet.
This doc lists them so the supervisor work can absorb them.
Each gap names the plan phase it most likely belongs to; that mapping is a suggestion, not a decision.

## Ground rule

The app is an additional workflow on top of the captain CLI.
Everything the app does is stored in the captain session, so the CLI sees every change made in the app; the app keeps no state of its own.
Every gap below must be closed in captain state, not in the client.

## Gaps

1. **Session hub** (Phase 2).
   The plan runs one supervisor per session and has nothing that lists sessions or starts one.
   The app has a project sidebar, so it needs to list projects and sessions, start, resume, rename and end them.
   Closed in `225dcbe`: `captain sessions` and the `sessions` method list, `rename` names, `shutdown` with `end` ends, and `captain supervise [--session ID]` starts and resumes.
2. **Durable transcripts** (Phase 2).
   The plan's `output` notifications are ephemeral, so crew activity and the captain conversation vanish on reconnect.
   The app needs `getTranscript(session, agent, range)` for scrollback and resumed sessions.
   Partial, `225dcbe` (confined to the crew's own session files in `31e0401`): `transcript` pages the Claude, Codex and pi session files for a crew or the captain, and `output` stays ephemeral.
   Grok has no transcript reader yet.
3. **Structured grant requests** (Phase 1a and 1b).
   The supervisor must link the gate's denial to the crew's ask and resolve both with one call.
   A request carries an id, crew, assignment, the exact command, working directory, gate reason and category, and the crew's own words.
   Closed in `6e05ed5`: the gate's denial becomes a grant request at the crew's next `ask`, and `resolve_grant` answers both.
4. **Captain authorization** (Phase 2).
   The plan holds `captain do` back until an authorization UX exists.
   The app's authorization card is that UX, and it needs a request event plus a `resolveAuthorization(requestId, approve or deny)` call.
   Closed in `74e542d` (`authorization` actions, `resolve_authorization`) and hardened in `4b92e36` (approvals bound to HEAD, git config, push target and Makefile hash, expiry and cooldown).
5. **`waitingOn` for questions** (Phase 2).
   The supervisor decides whether a question waits on the captain or the human, from delivery and turn state.
   Crew questions go to the captain first; only those it escalates or leaves unanswered reach the human.
   Closed in `74e542d` (`waiting_on` on each question, the `questions` method and `routed` actions) and `ff4c32c` (`captain escalate`).
6. **Operator notes** (Phase 3).
   The human may act on crew directly (tell, answer, interrupt, retier, dismiss).
   The captain must learn of each such action before its next turn.
   Closed in `ff4c32c`: operator notes are recorded in `protocol.json` and put in front of the captain's next turn.
7. **Protocol actions as events with ids** (Phase 2).
   Recruit, assign, mail, question, grant, report, result and lifecycle changes are emitted as events with stable ids.
   The UI must never parse them out of the captain's tool calls.
   Closed in `225dcbe`: the supervised-only `actions.jsonl` log with stable ids, streamed as the `actions` stream; `74e542d` and `ff4c32c` add the authorization, review and routing kinds.
8. **Per-assignment diff** (Phase 2).
   Review shows the diff for one assignment, ideally against a baseline taken when the assignment starts.
   Closed in `74e542d` (`baseline` at `begin`, the `diff` method), moved to a private object store in `ff4c32c` and made cheaper in `6768470`; supervised assignments only.
9. **Cascading interrupt** (Phase 1a, extended in Phase 3).
   "Stop captain and all crew" interrupts the captain and every working crew in one call.
   Closed in `6e05ed5` (`interrupt_all` for crew) and extended in `ff4c32c` to include the captain.
10. **Provider status and sign-in routes** (Phase 6).
    The app lists providers, rechecks them and starts sign-in, including device codes for Codex and Grok.
    Partial, `d5c0048`: `providers`, `sign_in` and `cancel_sign_in` are built, Claude and pi return guidance only, and Grok's signed-in status reads only `XAI_API_KEY` (otherwise unknown).
    Grok device auth and a real Grok are unchecked live.

## Requirements from the UI decisions

- **Reviewed state.** "Mark reviewed" on a report or result is recorded in the captain session by the supervisor, so the CLI sees it too.
  Closed in `74e542d` (`mark_reviewed`).
- **Full step output on demand.** Activity shows one-line step summaries; expanding a step loads its full output from the provider's own session history, which the supervisor must expose.
  Closed in `225dcbe` (`step`) for Claude, Codex and pi; not Grok.
- **Session lifetime.** Closing the app window leaves sessions running; quitting the app stops them, so the supervisor needs a clean stop-all path.
  Closed in `6e05ed5` (`shutdown` stops the captain, crew and sign-ins; a closed connection changes nothing).
- **Typed refusals.** Every command resolves to a typed result: the recorded outcome, or a refusal with its protocol code and message.
  Closed in `74e542d`: `command` returns a parsed `result` or a `refusal` with its code.
- **Full model catalogs.** The model picker lists every model a provider offers, with its reasoning effort levels, not only the cheap, mid and strong tiers.
  `listModels(provider)` returns that catalog as discovered from the provider (Codex app-server `model/list`, the Claude SDK's supported models, pi's model list, Grok's ACP models), and marks which model each tier resolves to.
  Open: no catalog method exists yet; `captain models` and `set_model` are all the supervisor offers.

## Client contract

The full list of methods and events the UI expects is in `docs/ui.md` section 11 of the `barbossa` repo.
The app reaches the backend only through its `SupervisorClient` interface, which turns these methods into the supervisor's `hello`, whitelisted `command` and `subscribe` calls.

## App integration status (2026-11-26)

The app now starts `captain supervise` itself, keeps the token in its main process and calls the socket methods in `docs/supervisor.md`.
A live test and a smoke launch passed against this checkout's `.venv/bin/captain` with no provider turns.
Until the methods below exist, the app shows real data only in a CLI inspector and marks everything else unavailable.
These block the normal session, crew, review and memory screens from running on real data, in priority order:

1. `hello` with protocol version, capabilities and provider status.
   Closed in `225dcbe` and `d5c0048` (`providers`).
2. Session discovery and a structured snapshot of one session (agents, assignments, mail, questions, grant requests, reviews, usage) with a cursor.
   Partial: discovery is closed in `225dcbe`; there is no single snapshot method, so a client replays `subscribe` from 0 and reads `questions`, `captain_requests`, `authorizations` and `usage`.
3. `subscribe` from a cursor, replaying events, deliveries, output and attention notifications without gaps.
   Closed in `225dcbe`; `output` and `attention` stay live only by design.
4. Whitelisted `command` with structured results, so the app never parses CLI text.
   Closed in `225dcbe` and `74e542d`.
5. Transcripts and full tool output for a turn.
   Closed in `225dcbe` and `31e0401` (not Grok).
6. The model catalog with effort levels, and settings.
   Open.
7. Phase 3: start, message, interrupt and restart the captain, captain choices, and ingestion before acknowledgement.
   Closed in `ff4c32c` and `d5c0048` (`start_captain`, `message_captain`, `interrupt_captain`, `stop_captain`, `captain_requests`, `answer_captain`, `resolve_captain_delivery`); a Grok captain is refused, and live captain turns are unchecked.
