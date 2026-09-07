# Captain Barbossa

[![CI](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml)

A small Python launcher for native Codex and Claude Code sessions inside **Herdr**.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git, then install Captain directly from GitHub. No manual clone is needed; uv downloads the package and installs its dependencies in an isolated environment.

```sh
uv tool install git+https://github.com/dev-preetamraj/captain-barbossa.git
captain                         # Ask which agent: Claude Code or Codex
captain --agent codex            # Explicitly choose Codex
captain --agent claude           # Claude Code in this pane
captain --prompt "Inspect this project"
```

If your shell cannot find `captain`, run `uv tool update-shell` and restart the terminal.

**Updating**

`captain --version` prints the installed version. To update an existing GitHub install, run:

```sh
uv tool upgrade captain-barbossa
```

uv re-resolves the Git source recorded at install time and installs the latest commit on the default branch, even when the version number has not changed. Reinstalling with `uv tool install --force git+https://github.com/dev-preetamraj/captain-barbossa.git` does the same. Running captains keep the old code until they are restarted with `captain --session <session-id>`.

Requires Python 3.11+, Herdr, and the chosen agent CLI, already signed in. Currently targets macOS/Linux. Launch it from an interactive terminal inside a Herdr workspace. It renames the current tab to **Captain Barbossa** and replaces itself with the native agent. Native input, history, permissions, and login stay with that agent.

Ask the captain to spin up a crew. Its startup instructions tell it to ask **Claude Code or Codex**, then **pane or tab**, and for a pane **vertical or horizontal**, wait for your choices, and run the crew command. Both directions list the panes in the captain's tab and ask which one to split. A vertical split opens the crew to the right of that pane; a horizontal split opens it below. You can also run the command directly from a shell associated with the Captain session:

```sh
captain crew --task "Review the current changes"
```

Every new crew gets a one-word Pirates of the Caribbean character name: **Jack**, **Will**, **Elizabeth**, **Gibbs**, **Anamaria**, **Pintel**, **Ragetti**, **Cotton**, **Marty**, **Tia**, **Davy**, or **Sao**. Names are assigned in that order, skipping names held by active crew or by launches that need attention. Dismissing crew returns the name to the pool, so the next recruit takes the lowest free roster name again (Jack after Jack is dismissed). Only when every roster name is held does numbering start at **Jack2**, keeping names to one word. Barbossa stays reserved for the captain. Properly cased names appear on panes/tabs and in the launch result, instructions, and memory. Command identifiers and filenames stay lowercase (`sparrow`, `will-turner`, `elizabeth`, etc.). You can request an available character explicitly with `captain crew gibbs --task "Review the current changes"`. Existing crew keep their names.

In an interactive shell, every choice uses a compact keyboard selector. Move with **↑/↓ or j/k**, press **Enter** to choose, or **Esc / Ctrl+C** to cancel. The highlighted row is only selected when you press Enter. An agent tool invocation without a terminal returns an instruction to ask you about any missing choice and creates nothing; for a pane placement that message lists the panes in the captain's tab by ID and label. After your answer, the captain supplies `--agent claude|codex`, `--placement pane|tab`, and for a pane `--direction vertical|horizontal` with `--split-pane <pane-id>`. Explicit flags count as your choices, so they do not trigger another question. A split pane must belong to the captain's tab.

New crew panes/tabs open in the same workspace and project without stealing focus. Captain saves a private shell launcher beside the session memory, then uses Herdr's `pane run` to submit a short command. Full instructions are read from that file rather than pasted through the terminal input buffer. Captain waits for Herdr to detect the selected native agent as ready, names it, and only then submits the task. The command returns the Herdr agent name and pane ID. Startup failures or native approval screens preserve the pane for inspection and do not retry or discard work. Crew share the checkout in this initial version. Both roles receive an editing contract: name the files each crew owns, give simultaneous writers disjoint files, serialize same-file work, re-read before editing, keep others' changes in place, and stage only owned files/hunks (never `git add -A` or repo-wide formatting). The contract is instruction only; nothing locks files.

Captain and crew receive instructions for their own roles. Tasks are sent verbatim;
the compact launch result omits the task echo, while `session.json` and graph memory
retain the full assignment.

Tell the captain **“focus on Jack”**, **“switch to Will”**, or **“take me to Elizabeth”** to bring that crew's pane and containing tab into focus. You can also use the command directly:

```sh
captain focus Jack
captain --session <session-id> focus Will
```

Names are case-insensitive; crew IDs and registered Herdr agent names also work. Lookup uses only the current Captain session. Focusing follows the registered agent if its pane moves, and does not send input or interrupt its work. An unknown or ambiguous name reports the available choices; an exited or closed agent reports a focus error. Existing captains can use the command immediately when told to. Restart with `captain --session <session-id>` to load the new navigation instructions while retaining the crew roster.

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

`memory show` prints every relationship as a compact `[subject, relation, object]`
JSON line, preserving full values without graph IDs or metadata. Use
`captain memory show --json` for the original raw graph format. Saved graphs and
Graphify queries are unchanged.

These commands run inside the launched agent's environment. From a separate Herdr shell in the same project/workspace, pass the session explicitly:

```sh
captain --session <session-id> memory show
captain --session <session-id> crew --task "Check boundary cases"
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

Checks cover agent, placement, direction, and split-pane choices, both Herdr creation paths, readiness and failure preservation, memory isolation, concurrent graph writes, corruption handling, and package invocation outside the checkout. A real pseudo-terminal check verifies that long instructions reach a stand-in native executable intact; a real Graphify query runs when installed. Herdr mutations and model sessions are mocked in automated tests; live native CLI startup still needs manual qualification.

Integration references: [Herdr CLI](https://herdr.dev/docs/cli-reference/), [Graphify CLI](https://graphify.com/docs/cli), [Codex additional instructions](https://learn.chatgpt.com/docs/config-file/config-reference).

Licensed under [MIT](LICENSE).
