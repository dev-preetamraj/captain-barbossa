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
repo; there is no tmux layer and no daemon except the app's opt-in `captain supervise`,
and the crew token-usage dashboard is a plain-text refresh loop in a Herdr pane, not a TUI.

## Layout

```text
src/captain_barbossa/
  cli.py          argparse entry point and dispatch: captain, crew, wait, tell, model,
                  focus, session, sessions, init, status, dashboard, dismiss, relayout, memory,
                  recover, escalate, supervise
  launch.py       native captain launch, launch plan, dashboard pane, exit pane cleanup
  agents.py       crew recruitment and create/wait/tell/model/focus/status/dismiss
  instructions.py role instructions and per-provider native CLI arguments
  config.py       settings layering, typed lookup, range checks
  defaults.toml   every shipped default, read as package data by config
  placement.py    recruitment defaults/sources, workspace discovery, and crew Spot
  profiles.py     explicit recruitment presets and flag precedence
  execution.py    crew execution roots and the active project (cwd guard)
  retirement.py   crew retirement, worktree disposal, worktree/result artifact commands
  worktrees.py    isolated crew checkout identity, creation and cleanup
  results.py      isolated result snapshots, preview, apply and discard
  lifetime.py     managed native-writer ownership, exit receipts, terminal closure
  native_history.py crew-incarnation native-history ownership and conservative cleanup
  history_manifest.py bounded, validated native-history manifest reads
  pi_history.py   crew-only pi session-boundary/identity extension and plugin denial gate
  plugins.py      plugin registry, protocol, mirror generations, launch context and denials
  layout.py       declared tab shapes: slot order, split target, even ratios
  crew.py         crew identities, roster queries, evidence-based state, native events
  pane.py         native agent terminal interaction and pane parsing
  protocol.py     assignment state, crew mail (enqueue/unread/mark_read/bounce), wait polling
  inspection.py   the `inspect` command: local file, state and Git reads
  safe_read.py    bounded no-follow file reads and hardened read-only Git
  recovery.py     read-only memory, assignment and pending-delivery digest for `recover`
  store.py        state and temp roots, storage dirs, atomic JSON read/write, locks
  events.py       native CLI hook entry, permission decisions, and mail delivery
  eventlog.py     hook event files: bounded lines, incarnation stamps, offset reads
  approval.py     pure assignment-scoped approval verdicts and reason codes
  sessions.py     session dirs, Session, crew naming
  retention.py    pruning old sessions with no live agent or unresolved work
  memory.py       graph memory in four scopes, repo rulebook, literal search
  models.py       provider-neutral model tiers and free-text matching
  runtime.py      herdr subprocess wrapper, JSON validation, current pane discovery
  dashboard.py    plain-text crew token-usage frame and refresh loop
  usage.py        tokens and cost read from the native CLI's own session file
  prompts.py      keyboard selectors for agent and placement choices
  onboarding.py   bootstrap Herdr when captain runs outside a workspace
  pi_captain.py   pi delivery bridge, shipped inside the wheel
  background.py   paneless read-only crew: detached owner process, cancel, outcome
  background_state.py background run files, owner liveness and status
  processes.py    bounded subprocess runs (process-group reaping), declared Makefile targets
  supervisor.py   `captain supervise`: per-session socket server owning supervised crew
  supervision.py  the CLI's token-gated door to that socket
  codex_app_server.py supervised Codex adapter over `codex app-server`
  claude_host.py  the user's claude through the Agent SDK, JSON-RPC on stdio
  claude_agent.py supervised Claude adapter over claude_host
  pi_rpc.py       supervised pi adapter over `pi --mode rpc`
  captain_bridge.py supervised captain: delivery bridge, claims, operator notes, requests
  attachments.py  files on a captain message: validation, recorded shape, provider prompt
  transcripts.py  paged transcripts and full steps from a provider's own session files
  jsonrpc.py      line-delimited JSON-RPC framing, deadlines, multiplexed Peer
  provider_status.py install, sign-in, hook trust and permission modes per agent CLI
  command_catalogue.py slash commands and skills per agent CLI, running or session-less
  update_check.py startup PyPI update check and `captain update`
tests/            unittest suite; Herdr and model sessions are mocked, a real PTY is used
docs/             plan.md is current scope; settings, placement, dashboard, memory,
                  crew-lifecycle and troubleshooting are the user guides; supervisor.md is
                  the app's socket contract
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
- User scope is the captain's cross-project preferences in `<state root>/user/graph.json`
  with its own lock: one value per subject/relation (`--supersede`, `memory forget`),
  total size capped at write (`[memory] user_limit`). `recovery.preferences` reads it
  apart from the other scopes and `digest` puts it first, so neither a broken graph
  nor `launch.RECOVERY_CHARS` truncation hides it; `[memory] user_enabled = false`
  opts a project out. Crew can neither read nor write it.
- Recruitment resolves flags/settings/session defaults through `Placement.resolve` under
  the crew metadata lock. Shipped crew agent follows the captain;
  `[crew] placement = "auto"` reports `defaults.toml`. `status` prints evidence only.
- Crew never stop at a native permission prompt; the captain owns grants. Hooks pass active
  incarnation grants to `approval.decide`, which stays pure. Claude crew launch with
  `--permission-mode auto`; PreToolUse (crew only, fires on every call) denies a flagged
  effect with a reason, never `ask`. PermissionRequest is total: allow when granted, else
  deny. Flagged: loss of work the assignment did not create, history/shared-ref rewrite,
  leaving the machine, boundary escape (sudo, outside checkout, protected files, captain
  state), another crew's files. The crew runs `ask`; the captain, with user
  authorization, runs `captain grant NAME --command TEXT --assignment ID`: a one-shot
  exact-command grant, consumed on first match, never inherited by handoff. Grants are a
  guard-rail, not a security boundary. Native edits with an edit grant claim unowned
  checkout paths under the allocation and protocol locks. Crew reach the captain CLI
  through the gate, limited to the protocol verbs and the exact interpreter in their
  instructions. Codex crew (above the version floor in `models.py`) get
  `check_for_update_on_startup=false` and four identical hooks (SessionStart, Stop,
  PreToolUse, PermissionRequest), no `approvals_reviewer`. pi's managed extension pipes a
  Claude-shaped PreToolUse to the same hook command and blocks on a deny. Grok has no hook:
  its auto mode is the whole gate, a documented gap.
  The pi extension fails closed (missing hook, timeout, non-zero exit or invalid output
  blocks); pi extension tools outside the tool map pass (known gap). Codex below the
  floor or with untrusted hooks falls back to native prompts. Owned paths authorize
  overwrite; temp is shared scratch across crew (known ceiling). Hook and captain commands always use
  the python3 sibling (`instructions.python_command`), since Codex trust hashes the literal
  command.
- Crew state is signalled by the native CLI's own hooks (Claude `--settings` hooks,
  Codex `notify`), which append JSON lines to `sessions/<id>/events/<crew>-<incarnation>.jsonl`.
  `wait` reads that file from the assignment's acknowledged offset; codex and pi panes
  are also checked for a modal. Filter Codex's title-generation turn. Claude's and supported
  Codex crew's `Stop` hooks block on an unfinished assignment with no pending question.
  Codex Stop requires a verified CLI version (0.164.0+) and user hook trust; notify remains
  the fallback. Codex hook commands are identical across launches, resolving identity from
  CAPTAIN_PROJECT/SESSION/CREW/INCARNATION, so one user review per hook survives new crew.
  Never write Codex's trust store or bypass hook trust.
  A crew awaiting an answer may stop; recursive Stop calls fail open. Pi cannot block.
- `assign`/`tell`/`answer` write mail (`sessions/<id>/mail/<crew>/<id>.json`). The body
  is the captain's text alone: `protocol.render` adds the identity block at read time
  and `protocol.receipt` is the one writer of `read`.
- `crew`/`assign --verify TARGET` requires `--allow test` and stores the Makefile target
  on the assignment. `done` runs it outside the protocol lock only when the crew gate
  allows that target (today `make test`), else the receipt says NOT RUN with the reason;
  it records a receipt shown by `wait` and verification never refuses `done`.
- A provider in `models.HOOK_DELIVERED` (claude) is handed its mail by its own hook on
  `SessionStart`/`UserPromptSubmit`/`Stop` as `additionalContext`, stamped only after
  the body is flushed. Its doorbell is payload-free (`pane.WAKE_LINE`) and it is never
  told to fetch mail. Everyone else gets the rendered body on the first ring; landing
  counts as delivery before `inbox`, which remains a manual re-read and receipt writer.
  A landed retry is content-free.
- A busy crew holds the ring, so mail sent mid-turn lands at the turn boundary. `done`
  receipts landed bodies and refuses with any body not yet served; Claude uses `Stop`.
  Dismissing delivered work still requires its report and unlanded checks.
- `captain do` is the write side of `captain inspect`: `commit`, `push`, `branch`,
  `switch`, `fetch`, `pull`, and `run <target>`. Not built on `inspection`'s
  hardened Git, which strips the user config, hooks and credentials a write needs;
  the captain runs operation requests directly with `do VERB`, never via crew, quiet,
  a model or a resolver. Outward-facing,
  destructive and history-rewriting actions require explicit user authorization; a
  concrete request authorizes only its stated scope. Every run appends a `{"type": "quiet", ...}` record to
  `events/captain.jsonl`, raised if it cannot be written, because work with no pane is
  reviewable only through that record.
- `crew --background` is read-only and the generated-instructions default for
  all delegated read-only work, even one bounded turn (`quiet` or foreground only on explicit request; writable work and
  unsupported requested providers never switch): a crew record with `execution:
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
- `captain supervise` is the one daemon: an app-launched, long-running process per
  session holding `supervisor.lock`, on a token-gated socket under `~/.captain/run`
  (never temp; token only on its readiness line). Only it sets session `transport:
  "supervisor"` (Herdr sessions refused); its crew get `execution: "supervised"`,
  run `codex app-server`, `claude_host` (Agent SDK, optional `[app]` extra) or `pi --mode
  rpc` under the lifetime guard, refuse to start with untrusted Codex hooks, and gate
  every approval through `captain hook`. Turns are untimed, every request bounded.
  `start_captain` runs the captain there too (not a crew, no lifetime record) with a
  scoped token reaching only the methods the captain CLI calls, so it
  cannot answer the user's own decisions. In a supervised session `captain do` and
  `quiet` outward/destructive verbs need an approved authorization request, spent by
  one run of that exact command; the app resolves it. Herdr commands and hooks never
  import the supervisor modules (lazy imports, pinned by a test). Methods, refusal
  codes and shapes: docs/supervisor.md.
- The delegation axis is judgment, not file-touching: `make test` writes nothing and
  needs no crew, while research changes no file and does.
- Settings layer bottom to top: `defaults.toml` (package data, the only place a default
  is written), `~/.captain/settings.toml`, the project's `.captain/settings.toml`, then
  CLI flags. Read a value with `config.lookup/text/flag/number` when the command needs
  it, never while `cli.parser()` is built.
- Crew use the captain checkout unless enabled checkout isolation selects a detached
  worktree. Shared writers need disjoint files; isolated results transfer owned paths only.
- Native-history cleanup is crew-incarnation-only. After authoritative writer shutdown
  it freezes usage, then targets only recorded local session identities; captain and
  unrelated histories are never candidates. Missing manifests/hooks, ambiguous or
  unobserved identity, unsupported providers, partial deletion and shared sidecars stay
  pending/partial/unsupported with no broad deletion; retained non-complete journals
  keep pruning conservative.
- Shared-checkout crew still use the generated editing contract (disjoint files,
  re-read before edit, stage only owned hunks). Nothing locks files or authorizes commits.
- Generated instructions are token-budgeted. Every added line costs context in every
  captain and crew session; prefer short rules over explanations.
- Plugins (`docs/plugins.md`) are registered only in home settings and read through
  `config.home_settings()`. Without a registration or a session `plugins` binding nothing
  imports `plugins.py`; hooks read crew denials from session meta, never home settings.
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
- Keep Captain and generated instruction state outside the repo. The one
  exception is `.captain/`: `settings.toml` and the curated `--scope repo` graph
  (`graph.json`) are committed on purpose.
- Comments are short and only for non-obvious "why"; no banners or restating code.
- One module owns one subject, named for what it owns, not for its callers. It may
  be long, never two subjects. Check in order: (1) if another module imports one
  name from a file and wants nothing else in it, that cluster is its own module;
  (2) if two things that must be read together sit more than ~400 lines apart, the
  file is two subjects; (3) if splitting makes the halves import each other's
  internals, leave it whole. Line count is a prompt for these checks, never a limit.
- Internal imports stay acyclic, local imports included; `tests/test_modules_import_without_cycles.py`
  enforces it. Move a shared piece down instead of importing upward.
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
