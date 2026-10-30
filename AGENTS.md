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
  execution.py    crew launch roots and managed retirement
  worktrees.py    isolated crew checkout identity, creation and cleanup
  results.py      isolated result snapshots, preview, apply and discard
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
  memory.py       graph memory in four scopes, repo rulebook, literal search
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
- User scope is the captain's cross-project preferences in `<state root>/user/graph.json`
  with its own lock: one value per subject/relation (`--supersede`, `memory forget`),
  total size capped at write (`[memory] user_limit`). `recovery.preferences` reads it
  apart from the other scopes and `digest` puts it first, so neither a broken graph
  nor `agents.RECOVERY_CHARS` truncation hides it; `[memory] user_enabled = false`
  opts a project out. Crew can neither read nor write it.
- Recruitment resolves flags/settings/session defaults through `Placement.resolve` under
  the crew metadata lock. Shipped crew agent follows the captain;
  `[crew] placement = "auto"` reports `defaults.toml`. `status` prints evidence only.
- Claude PreToolUse and PermissionRequest hooks pass active incarnation grants to
  `approval.decide`. PreToolUse is the gate, registered for crew only: it fires on every
  tool call, so a deny blocks and an escalation returns `ask`, which reaches the user even
  in auto mode. PermissionRequest alone could not enforce, because the native CLI only
  raises it when it already decided to prompt. PermissionRequest revalidates the
  current grant and may reuse the matching PreToolUse verdict only from a call-bound
  cache entry, consumed once under the events lock. Crew reach the captain CLI
  through the gate, limited to the protocol verbs and to the exact interpreter captain
  wrote into their instructions; `do`, `crew` and any other interpreter escalate. Resolved
  requests are logged as PermissionDecision, so wait does not announce an approval that is
  already handled. Other providers retain native/manual permissions.
- Crew state is signalled by the native CLI's own hooks (Claude `--settings` hooks,
  Codex `notify`), which append JSON lines to `sessions/<id>/events/<crew>-<incarnation>.jsonl`.
  `wait` reads that file from the assignment's acknowledged offset; codex and pi panes
  are also checked for a modal. Filter Codex's title-generation turn. Claude's `Stop` hook
  also blocks on an unfinished assignment with no pending question, so the crew stays
  active. A crew awaiting an answer may stop. Codex and pi cannot block; it fails open.
- `assign`/`tell`/`answer` write mail (`sessions/<id>/mail/<crew>/<id>.json`). The body
  is the captain's text alone: `protocol.render` adds the identity block at read time
  and `protocol.receipt` is the one writer of `read`.
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
