# AGENTS.md

Rules and orientation for AI agents working in this repository. Setup, checks, and
commit style are in [CONTRIBUTING.md](CONTRIBUTING.md); user-facing behavior is in
[README.md](README.md); current scope is in [docs/plan.md](docs/plan.md).

## What this is

Captain Barbossa is a small Python CLI (`captain`) that runs inside a Herdr
workspace. It launches a native agent CLI (Claude Code or Codex) as the **captain**,
and the captain recruits further native agents as **crew** in new Herdr panes or
tabs. Crew get one-word Pirates of the Caribbean names. All coordination happens
through generated role instructions and a small graph memory stored outside the
repo; there is no daemon, custom UI, or tmux layer.

## Layout

```text
src/captain_barbossa/
  cli.py       argparse entry point and dispatch (captain, crew, focus, dismiss, memory)
  agents.py    role instructions, native launch, crew create/wait/confirm/focus/dismiss
  memory.py    session/project dirs, locks, atomic JSON, graph memory, Graphify snapshots
  runtime.py   herdr subprocess wrapper, JSON validation, current pane discovery
  prompts.py   keyboard selectors for agent and placement choices
tests/         unittest suite; Herdr and model sessions are mocked, a real PTY is used
docs/          plan.md is current scope; archive/ is historical design only
```

Key facts:

- Herdr is driven only through `herdr` subprocesses in `runtime.herdr`, which
  validates the JSON response. Never shell out to Herdr elsewhere.
- Memory lives under `<OS temp>/captain-barbossa-<uid>/<project hash>/`, never in
  the checkout. `graph.json` there is project scope; `sessions/<id>/` is session scope.
- Crew share one checkout. Generated instructions carry the editing contract
  (disjoint files, re-read before edit, stage only owned hunks). Nothing locks files.
- Generated instructions are token-budgeted. Every added line costs context in every
  captain and crew session; prefer short rules over explanations.
- The version is defined once in `pyproject.toml` and read via `importlib.metadata`.

## Rules

- Every push to `main` must bump `version` in `pyproject.toml` (semver). Users
  update with `uv tool upgrade captain-barbossa`, so an unbumped push is invisible.
- Run the full gate before pushing:
  `uv run --locked pre-commit run --all-files --hook-stage pre-push`.
- Keep changes to the requested scope. Add a focused regression test for changed
  behavior; use temp directories outside the checkout for test state.
- Commits follow Conventional Commits with a subject under 72 characters. Never add
  `Co-Authored-By` or other AI attribution to commits or PRs.
- Keep Captain, Graphify, and generated instruction state outside the repo.
- Comments are short and only for non-obvious "why"; no banners or restating code.
