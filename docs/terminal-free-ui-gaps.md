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
2. **Durable transcripts** (Phase 2).
   The plan's `output` notifications are ephemeral, so crew activity and the captain conversation vanish on reconnect.
   The app needs `getTranscript(session, agent, range)` for scrollback and resumed sessions.
3. **Structured grant requests** (Phase 1a and 1b).
   The supervisor must link the gate's denial to the crew's ask and resolve both with one call.
   A request carries an id, crew, assignment, the exact command, working directory, gate reason and category, and the crew's own words.
4. **Captain authorization** (Phase 2).
   The plan holds `captain do` back until an authorization UX exists.
   The app's authorization card is that UX, and it needs a request event plus a `resolveAuthorization(requestId, approve or deny)` call.
5. **`waitingOn` for questions** (Phase 2).
   The supervisor decides whether a question waits on the captain or the human, from delivery and turn state.
   Crew questions go to the captain first; only those it escalates or leaves unanswered reach the human.
6. **Operator notes** (Phase 3).
   The human may act on crew directly (tell, answer, interrupt, retier, dismiss).
   The captain must learn of each such action before its next turn.
7. **Protocol actions as events with ids** (Phase 2).
   Recruit, assign, mail, question, grant, report, result and lifecycle changes are emitted as events with stable ids.
   The UI must never parse them out of the captain's tool calls.
8. **Per-assignment diff** (Phase 2).
   Review shows the diff for one assignment, ideally against a baseline taken when the assignment starts.
9. **Cascading interrupt** (Phase 1a, extended in Phase 3).
   "Stop captain and all crew" interrupts the captain and every working crew in one call.
10. **Provider status and sign-in routes** (Phase 6).
    The app lists providers, rechecks them and starts sign-in, including device codes for Codex and Grok.

## Requirements from the UI decisions

- **Reviewed state.** "Mark reviewed" on a report or result is recorded in the captain session by the supervisor, so the CLI sees it too.
- **Full step output on demand.** Activity shows one-line step summaries; expanding a step loads its full output from the provider's own session history, which the supervisor must expose.
- **Session lifetime.** Closing the app window leaves sessions running; quitting the app stops them, so the supervisor needs a clean stop-all path.
- **Typed refusals.** Every command resolves to a typed result: the recorded outcome, or a refusal with its protocol code and message.
- **Full model catalogs.** The model picker lists every model a provider offers, with its reasoning effort levels, not only the cheap, mid and strong tiers.
  `listModels(provider)` returns that catalog as discovered from the provider (Codex app-server `model/list`, the Claude SDK's supported models, pi's model list, Grok's ACP models), and marks which model each tier resolves to.

## Client contract

The full list of methods and events the UI expects is in `docs/ui.md` section 11 of the `barbossa` repo.
The app reaches the backend only through its `SupervisorClient` interface, which turns these methods into the supervisor's `hello`, whitelisted `command` and `subscribe` calls.

## App integration status (2026-11-26)

The app now starts `captain supervise` itself, keeps the token in its main process and calls the socket methods in `docs/supervisor.md`.
A live test and a smoke launch passed against this checkout's `.venv/bin/captain` with no provider turns.
Until the methods below exist, the app shows real data only in a CLI inspector and marks everything else unavailable.
These block the normal session, crew, review and memory screens from running on real data, in priority order:

1. `hello` with protocol version, capabilities and provider status.
2. Session discovery and a structured snapshot of one session (agents, assignments, mail, questions, grant requests, reviews, usage) with a cursor.
3. `subscribe` from a cursor, replaying events, deliveries, output and attention notifications without gaps.
4. Whitelisted `command` with structured results, so the app never parses CLI text.
5. Transcripts and full tool output for a turn.
6. The model catalog with effort levels, and settings.
7. Phase 3: start, message, interrupt and restart the captain, captain choices, and ingestion before acknowledgement.
