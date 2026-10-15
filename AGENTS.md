# AGENTS.md

Rules and orientation for AI agents working in this repository. Setup, checks, and
commit style are in [CONTRIBUTING.md](CONTRIBUTING.md); user-facing behavior is in
[README.md](README.md); current scope is in [docs/plan.md](docs/plan.md).

## What this is

Captain Barbossa is a small Python CLI (`captain`) that runs inside a Herdr
workspace. It launches a native agent CLI (Claude Code, Codex, pi, grok) as the **captain**,
and the captain recruits further native agents as **crew** in new Herdr panes or
tabs. Crew get one-word Pirates of the Caribbean names. All coordination happens
through generated role instructions and a small graph memory stored outside the
repo; there is no daemon or tmux layer, and the crew token-usage dashboard is a
plain-text refresh loop in a Herdr pane, not a TUI.

## Layout

```text
src/captain_barbossa/
  cli.py          argparse entry point and dispatch: captain, crew, wait, tell, model,
                  focus, session, init, status, dashboard, dismiss, memory, recover
  agents.py       native launch, dashboard pane, crew create/wait/tell/model/focus/status/dismiss
  instructions.py role instructions and per-provider native CLI arguments
  config.py       settings layering, typed lookup, range checks
  defaults.toml   every shipped default, read as package data by config
  placement.py    recruitment defaults/sources, workspace discovery, and crew Spot
  profiles.py     explicit recruitment presets and flag precedence
  execution.py    stable project identity to per-crew execution-root/artifact routing
  worktrees.py    disposable detached checkouts, identities, validation, removal
  results.py      immutable result snapshots and explicit unstaged transfer/discard
  lifetime.py     managed native-writer ownership, exit receipts, terminal closure
  native_history.py crew-incarnation native-history ownership and conservative cleanup
  pi_history.py   crew-only pi session-boundary/identity extension
  layout.py       declared tab shapes: slot order, split target, even ratios
  crew.py         crew identities, roster queries, evidence-based state, native events
  pane.py         native agent terminal interaction and pane parsing
  protocol.py     assignment state, crew mail (enqueue/unread/mark_read/bounce), wait polling
  inspection.py   bounded local file, state and Git reads for `inspect`
  recovery.py     read-only memory, assignment and pending-delivery digest for `recover`
  store.py        state and temp roots, storage dirs, atomic JSON read/write, locks
  events.py       native CLI hook event stream, permission decisions, and mail delivery
  approval.py     pure assignment-scoped approval verdicts and reason codes
  sessions.py     session dirs, Session, crew naming, live agents, prune
  memory.py       graph memory in three scopes, repo rulebook, Graphify snapshot
  models.py       provider-neutral model tiers and free-text matching
  runtime.py      herdr subprocess wrapper, JSON validation, current pane discovery
  dashboard.py    plain-text crew token-usage frame and refresh loop
  usage.py        tokens and cost read from the native CLI's own session file
  prompts.py      keyboard selectors for agent and placement choices
  onboarding.py   bootstrap Herdr when captain runs outside a workspace
  pi_captain.py   pi delivery bridge, shipped inside the wheel
  background.py   paneless read-only crew: detached owner process, liveness, cancel, outcome
  update_check.py startup PyPI update check and `captain update`
tests/            unittest suite; Herdr and model sessions are mocked, a real PTY is used
docs/             plan.md is current scope; settings, placement, dashboard, memory,
                  crew-lifecycle and troubleshooting are the user guides
```

Key facts:

- Herdr is driven only through `herdr` subprocesses in `runtime.herdr`, which
  validates the JSON response. Never shell out to Herdr elsewhere.
- Memory lives outside the checkout: project-scope `graph.json` and new sessions'
  `sessions/<id>/` under `~/.local/state/captain-barbossa/<project hash>/` (or
  `$XDG_STATE_HOME`). Sessions made earlier under `<OS temp>/captain-barbossa-<uid>/`
  are opened in place via `sessions.locate`; each id has one directory, and
  enumeration (`sessions.session_directories`), allocation and prune cover both roots.
  `CAPTAIN_MEMORY_ROOT` overrides both roots at once. Repo scope is the exception:
  `.captain/graph.json` in the checkout, written only by an explicit
  `memory add --scope repo`, read without migration, locked from the state root.
  Its write path is deterministic, not model judgement: the relation comes from
  `memory.REPO_RELATIONS` (the single place it is defined), `--because` is required,
  ids hash the label and rows are sorted so the same facts give byte-identical bytes
  and re-adding one is a no-op, and changing one needs `--supersede`.
  `memory init` seeds it from AGENTS.md/CLAUDE.md by a fixed markdown parse (bullets
  under Rules/Key facts sections), previewing until `--apply`; it only ever adds.
- Recruitment resolves flags/settings/session defaults through `Placement.resolve` under
  the crew metadata lock. `status` uses `Crew.current_state` and prints state/source/detail;
  missing evidence is unknown, not inferred completion.
- Claude PreToolUse and PermissionRequest hooks pass active incarnation grants to
  `approval.decide`. PreToolUse is the gate, registered for crew only: it fires on every
  tool call, so a deny blocks and an escalation returns `ask`, which reaches the user even
  in auto mode. PermissionRequest alone could not enforce, because the native CLI only
  raises it when it already decided to prompt. One call is decided once: PermissionRequest
  reuses the matching PreToolUse verdict under the events lock. Crew reach the captain CLI
  through the gate, limited to the protocol verbs and to the exact interpreter captain
  wrote into their instructions; `do`, `crew` and any other interpreter escalate. Resolved
  requests are logged as PermissionDecision, so wait does not announce an approval that is
  already handled. Other providers retain native/manual permissions.
- Crew state is signalled by the native CLI's own hooks (Claude `--settings` hooks,
  Codex `notify`), which append JSON lines to `sessions/<id>/events/<crew>.jsonl`.
  `wait` tails that file from a `.cursor` offset; pane reading is only a fallback for
  when no event has arrived. Filter Codex's title-generation turn. Claude's `Stop` hook
  also blocks on an unfinished assignment with no pending question, so the crew stays
  active. A crew awaiting an answer may stop. Codex and pi cannot block; it fails open.
- `assign`/`tell`/`answer` write mail (`sessions/<id>/mail/<crew>/<id>.json`). The body
  is the captain's text alone: `protocol.render` adds the identity block at read time
  and `protocol.receipt` is the one writer of `read`.
- A provider in `models.HOOK_DELIVERED` (claude) is handed its mail by its own hook on
  `SessionStart`/`UserPromptSubmit`/`Stop` as `additionalContext`, stamped only after
  the body is flushed. Its doorbell is payload-free (`pane.WAKE_LINE`) and it is never
  told to fetch mail. Everyone else gets the rendered body on the first ring and writes
  the receipt by running `captain inbox NAME`, which stays for every provider as a
  manual re-read; a landed retry is content-free.
- A busy crew holds the ring, so mail sent mid-turn lands at the turn boundary: the
  `done` refusal carries the body when the crew reports, the `Stop` hook when it does
  not. Dismissing a crew with unread mail bounces it with a reason.
- `captain do` is the write side of `captain inspect`: `commit`, `push`, `branch`, and
  `run <target>` for a literal target declared in the root Makefile. Not built on `inspection`'s
  hardened Git, which strips the user config, hooks and credentials a write needs;
  `do resolve --intent TEXT` uses ordered full-input rules in `do.py`, without executing
  or granting approval. Unresolved requests report reason codes and accepted shapes. Outward-facing,
  destructive and history-rewriting actions require explicit user authorization; a
  concrete request authorizes only its stated scope. Every run appends a `{"type": "quiet", ...}` record to
  `events/captain.jsonl`, raised if it cannot be written, because work with no pane is
  reviewable only through that record.
- `crew --background` is opt-in and read-only: a crew record with `execution:
  "background"` and no pane, running `models.headless_argv(isolated=True)` (codex
  and pi only; claude and grok refused; quiet never passes `isolated`) under a
  detached `background.py` owner. The roster entry precedes `protocol.begin`, which runs
  under the recruiter's launch flock; claim and withdrawal are renames of one intent
  file. Recruiter and owner flocks are liveness (never a PID or a clock); the one clock
  only expires intent unclaimed past twice the startup window; a guard leads the
  native group, survives TERM and the CLI's exit, and kills the group when the
  owner's lifeline pipe closes; only the owner signals that group, before reaping the
  guard, and only after KILL.
  Exactly one outcome is written; failures, timeouts, cancels and a lost owner
  close the assignment with an `error` notice and `outcome`, never a `done`. No pane
  reads, doorbell, `tell` or `answer` for it.
- The delegation axis is judgment, not file-touching: `make test` writes nothing and
  needs no crew, while research changes no file and does.
- Settings layer bottom to top: `defaults.toml` (package data, the only place a default
  is written), `~/.captain/settings.toml`, the project's `.captain/settings.toml`, then
  CLI flags. Read a value with `config.lookup/text/flag/number` when the command needs
  it, never while `cli.parser()` is built.
- Supported Git projects default new crew to a detached worktree at the captain
  checkout's local `HEAD`; `--checkout shared` and profile `checkout = "shared"` are
  explicit opt-ins to the old shared-root behavior. Dirty/staged/untracked parent state
  is absent from an isolated root. The captain checkout remains the project/session and
  result destination identity; `execution.root` resolves only the launch-bound crew
  incarnation. Non-Git projects fall back to shared unless worktree isolation was
  explicitly required. Linked-worktree parents and unsupported object-store/config
  layouts are refused, never normalized.
- Isolated output is collected into an immutable result and transferred only by an
  explicit `result apply`; application leaves files unstaged and refuses destination
  overlap or uncertainty. Worktree cleanup requires authoritative `lifetime` evidence,
  a final unchanged result, and verified apply/discard settlement. Retain on every
  unknown writer, partial transfer, conflict, or unsupported content case.
- Native-history cleanup is crew-incarnation-only. After authoritative writer shutdown
  it freezes usage, then targets only recorded local session identities; captain and
  unrelated histories are never candidates. Missing manifests/hooks, ambiguous or
  unobserved identity, unsupported providers, partial deletion and shared sidecars stay
  pending/partial/unsupported with no broad deletion; retained non-complete journals
  keep pruning conservative.
- Shared-checkout crew still use the generated editing contract (disjoint files,
  re-read before edit, stage only owned hunks). Nothing locks files. A worktree separates
  working files but is not a security sandbox and does not authorize commits.
- Generated instructions are token-budgeted. Every added line costs context in every
  captain and crew session; prefer short rules over explanations.
- The version is defined once in `pyproject.toml` and read via `importlib.metadata`.

## Rules

- The package is on PyPI as `captain-barbossa`. Every push to `main` must bump
  `version` in `pyproject.toml` (semver); users update with
  `captain update` (`uv tool upgrade captain-barbossa`), so an unbumped push is invisible.
- Run the full gate before pushing:
  `uv run --locked pre-commit run --all-files --hook-stage pre-push`.
- Keep changes to the requested scope. Add a focused regression test for changed
  behavior; use temp directories outside the checkout for test state.
- Commits follow Conventional Commits with a subject under 72 characters. Never add
  `Co-Authored-By` or other AI attribution to commits or PRs.
- Keep Captain, Graphify, and generated instruction state outside the repo. The one
  exception is `.captain/`: `settings.toml` and the curated `--scope repo` graph
  (`graph.json`) are committed on purpose.
- Comments are short and only for non-obvious "why"; no banners or restating code.
- One module owns one subject, named for what it owns, not for its callers. It may
  be long, never two subjects. Check in order: (1) if another module imports one
  name from a file and wants nothing else in it, that cluster is its own module;
  (2) if two things that must be read together sit more than ~400 lines apart, the
  file is two subjects; (3) if splitting makes the halves import each other's
  internals, leave it whole. Line count is a prompt for these checks, never a limit.
- The package stays flat: no subpackages. Moved names get no re-export shim; every
  caller moves in the same commit.
- Tests: one file per behaviour, named for what it checks, never for a component or
  the product. Add to the file whose name already describes it; create
  `test_<behaviour>.py` only when none does. Each file carries its own `setUp`.

### Branching and releases

- Cut feature branches from `main`. Never commit directly to `main` or `uat`.
- Merge a feature into `uat` to publish `X.Y.Z.devN` to TestPyPI (`release.yml`,
  environment `testpypi`); no version bump is needed there.
- When it is good, merge the feature into `main` with a version bump, then tag
  `vX.Y.Z` to publish to PyPI (environment `pypi`).
- `uat` is disposable and may be reset to `main` at any time.
- Releases are tag-driven and use trusted publishing; there are no tokens.
- Use the Makefile targets where they exist: `make gate`, `make bump VERSION=x.y.z`,
  `make uat`, `make release`.
