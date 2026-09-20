# AGENTS.md

Rules and orientation for AI agents working in this repository. Setup, checks, and
commit style are in [CONTRIBUTING.md](CONTRIBUTING.md); user-facing behavior is in
[README.md](README.md); current scope is in [docs/plan.md](docs/plan.md).

## What this is

Captain Barbossa is a small Python CLI (`captain`) that runs inside a Herdr
workspace. It launches a native agent CLI (Claude Code, Codex, or pi) as the **captain**,
and the captain recruits further native agents as **crew** in new Herdr panes or
tabs. Crew get one-word Pirates of the Caribbean names. All coordination happens
through generated role instructions and a small graph memory stored outside the
repo; there is no daemon or tmux layer, and the crew token-usage dashboard is a
plain-text refresh loop in a Herdr pane, not a TUI.

## Layout

```text
src/captain_barbossa/
  cli.py          argparse entry point and dispatch: captain, crew, wait, tell, model,
                  focus, session, init, status, dashboard, dismiss, memory
  agents.py       native launch, dashboard pane, crew create/wait/tell/model/focus/status/dismiss
  instructions.py role instructions and per-provider native CLI arguments
  config.py       settings layering, typed lookup, range checks
  defaults.toml   every shipped default, read as package data by config
  placement.py    workspace pane discovery and the Spot a crew opens in
  layout.py       declared tab shapes: slot order, split target, even ratios
  crew.py         crew identities, roster queries, native lifecycle events
  pane.py         native agent terminal interaction and pane parsing
  memory.py       session/project dirs, locks, atomic JSON, graph memory, hook events, Graphify
  models.py       provider-neutral model tiers and free-text matching
  runtime.py      herdr subprocess wrapper, JSON validation, current pane discovery
  dashboard.py    plain-text crew token-usage frame and refresh loop
  usage.py        tokens and cost read from the native CLI's own session file
  prompts.py      keyboard selectors for agent and placement choices
  onboarding.py   bootstrap Herdr when captain runs outside a workspace
  pi_captain.py   pi delivery bridge, shipped inside the wheel
  update_check.py startup PyPI update check
tests/            unittest suite; Herdr and model sessions are mocked, a real PTY is used
docs/             plan.md is current scope; settings, placement, dashboard, memory,
                  crew-lifecycle and troubleshooting are the user guides
```

Key facts:

- Herdr is driven only through `herdr` subprocesses in `runtime.herdr`, which
  validates the JSON response. Never shell out to Herdr elsewhere.
- Memory lives outside the checkout, split by durability: project-scope `graph.json`
  under `~/.local/state/captain-barbossa/<project hash>/` (or `$XDG_STATE_HOME`),
  session-scope `sessions/<id>/` under `<OS temp>/captain-barbossa-<uid>/<project hash>/`.
  `CAPTAIN_MEMORY_ROOT` overrides both roots at once.
- Crew state is signalled by the native CLI's own hooks (Claude `--settings` hooks,
  Codex `notify`), which append JSON lines to `sessions/<id>/events/<crew>.jsonl`.
  `wait` tails that file from a `.cursor` offset; pane reading is only a fallback for
  when no event has arrived. Filter Codex's title-generation turn.
- Settings layer bottom to top: `defaults.toml` (package data, the only place a default
  is written), `~/.captain/settings.toml`, the project's `.captain/settings.toml`, then
  CLI flags. Read a value with `config.lookup/text/flag/number` when the command needs
  it, never while `cli.parser()` is built.
- Crew share one checkout. Generated instructions carry the editing contract
  (disjoint files, re-read before edit, stage only owned hunks). Nothing locks files.
- Generated instructions are token-budgeted. Every added line costs context in every
  captain and crew session; prefer short rules over explanations.
- The version is defined once in `pyproject.toml` and read via `importlib.metadata`.

## Rules

- The package is on PyPI as `captain-barbossa`. Every push to `main` must bump
  `version` in `pyproject.toml` (semver); users update with
  `uv tool upgrade captain-barbossa`, so an unbumped push is invisible.
- Run the full gate before pushing:
  `uv run --locked pre-commit run --all-files --hook-stage pre-push`.
- Keep changes to the requested scope. Add a focused regression test for changed
  behavior; use temp directories outside the checkout for test state.
- Commits follow Conventional Commits with a subject under 72 characters. Never add
  `Co-Authored-By` or other AI attribution to commits or PRs.
- Keep Captain, Graphify, and generated instruction state outside the repo.
- Comments are short and only for non-obvious "why"; no banners or restating code.

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
