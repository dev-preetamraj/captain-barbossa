# Captain Barbossa

[![CI](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml)

A small Python launcher for native Codex and Claude Code sessions inside **Herdr**.

```sh
uv tool install --editable .
captain                         # Ask which agent: Claude Code or Codex
captain --agent codex            # Explicitly choose Codex
captain --agent claude           # Claude Code in this pane
captain --prompt "Inspect this project"
```

Requires Python 3.11+, Herdr, and the chosen agent CLI, already signed in. Currently targets macOS/Linux. Launch it from an interactive terminal inside a Herdr workspace. It renames the current tab to `captain barbossa` and replaces itself with the native agent. Native input, history, permissions, and login stay with that agent.

Ask the captain to spin up a crew. Its startup instructions tell it to ask **Claude Code or Codex**, then **pane or tab**, wait for your choices, and run the crew command. You can also run the command directly from a shell associated with the Captain session:

```sh
captain crew reviewer --task "Review the current changes"
```

In an interactive shell, both choices use a compact keyboard selector. Move with **↑/↓ or j/k**, press **Enter** to choose, or **Esc / Ctrl+C** to cancel. The highlighted row is only selected when you press Enter. An agent tool invocation without a terminal returns an instruction to ask you about any missing choice and creates nothing. After your answer, the captain supplies `--agent claude|codex` and `--placement pane|tab`. Explicit flags count as your choices, so they do not trigger another question.

New crew panes/tabs open in the same workspace and project without stealing focus. Captain saves a private shell launcher beside the session memory, then uses Herdr's `pane run` to submit a short command. Full instructions are read from that file rather than pasted through the terminal input buffer. Captain waits for Herdr to detect the selected native agent as ready, names it, and only then submits the task. The command returns the Herdr agent name and pane ID. Startup failures or native approval screens preserve the pane for inspection and do not retry or discard work. Crew share the checkout in this initial version; give simultaneous writers separate file assignments.

**Memory is a graph outside the repository.** Captain stores graph relationships and launch metadata here:

```text
<OS temp>/captain-barbossa-<uid>/<hash of canonical project path>/
  graph.json                      # explicitly saved project facts
  sessions/<session-id>/
    session.json                  # workspace and crew references
    captain.json
    crew-<name>.sh                # native launcher, including role instructions
    graph.json                    # only this session's memory
```

Git repositories use their checkout root as project identity; other directories use the launch directory. New launches get new session IDs. Crew inherit their captain's project and session. Only explicitly saved project facts carry into other sessions. Directories are private to your OS user; graph updates use file locks and atomic replacement.

```sh
captain memory path
captain memory add "rate limiter" "uses" "per-user windows"
captain memory add "project" "test command" "python -m unittest" --scope project
captain memory show
captain memory query "rate limiter"
```

These commands run inside the launched agent's environment. From a separate Herdr shell in the same project/workspace, pass the session explicitly:

```sh
captain --session <session-id> memory show
captain --session <session-id> crew tester --task "Check boundary cases"
captain --session <session-id> --agent codex
```

`--session` reuses Captain's graph memory; it starts a fresh native conversation, not a provider transcript resume. The agents receive instructions to consult memory and save meaningful decisions/handoffs. Captain does not automatically transcribe conversations or guarantee that a model records every fact.

Graphify is optional for graph queries: install it with `uv tool install graphifyy` if needed. `memory query` invokes the installed `graphify query` against an isolated snapshot of project and current-session memory. Graphify output/cache goes into that temporary directory, query logging is disabled, and the snapshot is removed afterwards. No Graphify installation hooks, repo files, code extraction, API calls, or global graphs are configured by Captain. Graph relationships can still be added/read without Graphify.

The OS may clear its temp folder. Set `CAPTAIN_MEMORY_ROOT` to another **outside-repo** location before starting Captain if you want different retention. Provider-native histories and credentials continue to use the providers' own storage.

**Development**

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, commit conventions, and the pull request workflow. The package uses a `src/` layout with locked development dependencies:

```sh
uv sync --locked
uv run --locked pre-commit install
uv run --locked pre-commit run --all-files --hook-stage pre-push
```

The installed hooks check formatting and lint before commits, validate commit messages, and run formatting, lint, and tests before pushes. To refresh an existing tool installation after changes to packaging or entry points, run `uv tool install --editable . --force`.

```text
captain-barbossa/
├── pyproject.toml              # package metadata, build configuration, CLI entry point
├── uv.lock                     # reproducible development dependencies
├── README.md
├── CONTRIBUTING.md
├── LICENSE                     # MIT
├── .pre-commit-config.yaml      # commit and push checks
├── .github/                    # CI, issue and pull request templates
├── src/
│   └── captain_barbossa/
│       ├── __init__.py
│       ├── __main__.py         # python -m captain_barbossa
│       ├── cli.py              # argument parsing and command dispatch
│       ├── agents.py           # native captain and crew launching
│       ├── prompts.py          # styled keyboard selectors
│       ├── runtime.py          # external CLIs and Herdr workspace discovery
│       └── memory.py           # project/session storage and Graphify queries
├── tests/
│   ├── __init__.py
│   ├── test_captain.py          # flow, storage, and package invocation checks
│   └── test_prompts.py          # real selector key handling
└── docs/
    ├── plan.md                 # current implementation scope
    └── archive/
        ├── plan-v1.md          # original UI proposal
        └── assets/             # preserved UI concepts and prompts
```

See the [current plan](docs/plan.md) for scope and the [archived proposal](docs/archive/plan-v1.md) for design history. Runtime memory stays outside this tree.

Checks cover agent and placement choices, both Herdr creation paths, readiness and failure preservation, memory isolation, concurrent graph writes, corruption handling, and package invocation outside the checkout. A real pseudo-terminal check verifies that long instructions reach a stand-in native executable intact; a real Graphify query runs when installed. Herdr mutations and model sessions are mocked in automated tests; live native CLI startup still needs manual qualification.

Integration references: [Herdr CLI](https://herdr.dev/docs/cli-reference/), [Graphify CLI](https://graphify.com/docs/cli), [Codex additional instructions](https://learn.chatgpt.com/docs/config-file/config-reference).

Licensed under [MIT](LICENSE).
