# Captain Barbossa

[![CI](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/captain-barbossa)](https://pypi.org/project/captain-barbossa/)

Captain Barbossa launches a native agent CLI (Claude Code, Codex, or pi) as a
**captain** inside a [Herdr](https://herdr.dev) workspace. The captain recruits
further native agents as **crew** in new Herdr panes or tabs, so a team of
native agent sessions can work on the same checkout at once. There is no
daemon, custom UI, or tmux layer: everything runs through Herdr, plus a small
graph memory stored outside the repo.

## Requirements

- macOS or Linux, Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Herdr](https://herdr.dev/docs/cli-reference/), as your terminal workspace
- Claude Code, Codex, and/or pi, installed and already signed in

## Install

```sh
uv tool install captain-barbossa
```

If your shell cannot find `captain` afterward, run `uv tool update-shell` and
restart the terminal.

## Upgrade

```sh
uv tool upgrade captain-barbossa
```

Running captains keep the old code until restarted with
`captain --session <session-id>`.

## Quickstart

Run these in an interactive Herdr workspace:

```sh
captain --agent codex
# Ask the captain: "Recruit crew to review the current changes."
# Then ask: "Wait for Jack."
```

The captain stays in the current pane and recruits crew into the declared tab
layout. You can ask it for every crew operation in plain language or run the
commands below from a shell attached to the same session.

## Restarting a captain

Find the id with `captain session` before you exit. Then exit the running
agent with `/exit` or Ctrl+D, and rerun:

```sh
captain --session <session-id>
```

Graph memory carries over; the chat transcript does not. You get a fresh
native conversation, not a provider transcript resume. Use `captain session`
to print the ID; `--session ID` selects it when restarting. See
[memory.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/memory.md)
for what persists.

## Starting a captain

Launch `captain` from an interactive terminal inside a Herdr workspace:

```sh
captain                         # asks: Claude Code, Codex, or pi?
captain --agent claude          # Claude Code in this pane
captain --agent codex           # Codex in this pane
captain --agent pi              # pi in this pane
captain --prompt "Inspect this project"
```

`--agent claude|codex|pi` selects the native CLI, `--prompt TEXT` supplies its
first task, `--session ID` resumes Captain state, and `--no-dashboard` skips an
enabled dashboard for this launch.

It renames the current tab to **Captain Barbossa** and replaces itself with
the chosen native CLI, so native input, history, permissions, and login all
stay with that agent.

Outside a Herdr workspace, `captain` offers to bootstrap one instead of
failing. If Herdr is missing it asks before running Herdr's installer
(`curl -fsSL https://herdr.dev/install.sh | sh`); then it opens a Herdr
workspace at the project, starts captain there with the same `--agent`,
`--session`, and `--prompt`, and attaches your terminal to it. Declining, or
running non-interactively, leaves the old error untouched.

See [settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for launch defaults and
[troubleshooting.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/troubleshooting.md)
for launch failures.

## Recruiting crew

`captain crew` opens a native agent in the same workspace and project, then
submits its task without stealing focus. `NAME` is optional; flags select the
agent, model, pane or tab, split target, and direction.

```sh
captain crew --task "Review the current changes"
captain crew Gibbs --task "Review the current changes" --agent claude --model mid
```

With no preferences, the captain uses its own CLI, automatic placement, and
the `cheap` tier. Only the captain recruits; crew forward delegation requests
back to it. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for naming, launch behavior, failure handling, and shared-checkout guardrails.

## Placing crew

Automatic recruits fill `[placement] captain_tab`, then existing crew tabs,
then a new `[placement] crew_tab`. The declared arrays are columns whose
numbers are pane counts; crew fill breadth first.

```sh
captain crew Gibbs --task "Review UI" --placement pane \
  --split-pane auto --direction vertical
```

`--placement pane|tab`, `--split-pane ID|auto`, and
`--direction vertical|horizontal|auto` override the grid for one recruit. See
[placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md)
for slot order, split ratios, dismissal behavior, and validation.

## Choosing models

`--model` accepts a provider-neutral tier, model ID, or alias:

| Tier     | Claude Code       | Codex                |
|----------|-------------------|-----------------------|
| `cheap`  | claude-haiku-4-5  | gpt-5.6-luna          |
| `mid`    | claude-sonnet-5   | gpt-5.6-sol           |
| `strong` | claude-opus-5     | gpt-6-astra           |

**`cheap` is the default**: omitting `--model` recruits a `cheap` crew rather
than falling through to whatever the native CLI is configured to use, so
routine work never silently lands on an expensive model. `cheap` covers
commits, tests, lint, formatting, docs, chores, renames, and mechanical
edits. `mid` is for a normal feature or a change inside one area, `strong`
for design, debugging, or multi-file and long-context work.

The captain is instructed not to step up just because a task feels ambiguous,
risky, or important: it steps up only when you ask for a stronger model, or
after a cheap crew has already failed or stalled. Retier a running crew in
place with `captain model <name> mid|strong` rather than recruiting high
up front.

Free text also works and is matched to the closest model the chosen CLI
offers (exact IDs and aliases first, then prefixes, substrings, and close
spellings): Claude Code additionally offers `claude-fable-5-1` (fable);
Codex additionally offers `gpt-5.6-terra` (terra) and `gpt-5.5`. Ambiguous
or unknown text reports the options and creates nothing.

```sh
captain crew --task "Debug the failure" --model strong
```

See [settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for retiering providers and setting model defaults.

## Configuring defaults

Nothing is required. Project settings override global settings one key at a
time, and CLI flags win for one invocation. `init` writes a fully commented
template and never overwrites an existing file.

```sh
captain init
captain init --global
```

The only flag is `--global`; without it the command writes
`<project>/.captain/settings.toml`, otherwise `~/.captain/settings.toml`. See
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for precedence, validation, and the complete key list.

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
```

`wait` blocks until the crew is done, idle, or blocked and prints its report.
`--timeout SECONDS` overrides the 900-second default. It follows native
lifecycle events, with pane-tail fallbacks documented in
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md).

## Sending a follow-up

```sh
captain tell Jack "also update the changelog"
```

`tell` has no flags. It prompts an existing crew in place while keeping its
pane, model, and conversation; dismissed crew are refused. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for assignment and event-cursor behavior.

## Checking crew status

```sh
captain status
captain status --all
```

`status` prints each crew's name, provider, model, pane, live status, and task.
`--all` includes dismissed crew. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for fallbacks and output details.

## Watching crew token usage

```sh
captain dashboard
captain dashboard --interval 5
```

`captain dashboard` refreshes a plain-text table of the session's crew in the
current pane every 2 seconds by default (`--interval SECONDS` to change that).

A captain can also open it for you: set `[dashboard] enabled = true` in
`.captain/settings.toml` and launching `captain` splits a second pane below
itself, titled **Dashboard**, running the same table. It is off by default;
`captain --no-dashboard` skips it for one launch even when it is enabled.

```text
NAME    AGENT               STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5       idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra idle     #....  11%    130k  $0.07   $0.05
Will    claude/haiku-4-5    idle     ##...  31%    366k  $0.09   $0.00
TOTAL   -                   -        -            6.30M  $3.38   $1.89
TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded
```

The table distinguishes live context from cumulative tokens and list-price
cost, keeps dismissed crew in totals, and marks unknown readings instead of
inventing zeroes. See
[dashboard.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/dashboard.md)
for every column, pricing and context sources, provider limits, totals, and
responsive pane behavior.

## Focusing crew

Tell the captain "focus on Jack", "switch to Will", or "take me to
Elizabeth", or run the command directly:

```sh
captain focus Jack
```

Names are case-insensitive; crew IDs and Herdr agent names also work.
`focus` has no flags and never sends input or interrupts work. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for pane moves and name resolution.

## Switching a running crew's model

Ask the captain to step a crew up or down a tier when its model stops fitting
the work, or run the command directly:

```sh
captain model Jack strong
captain model Will cheap
```

The model argument accepts a tier, model ID, or alias. The command verifies
the native CLI's confirmation and keeps the pane, conversation, and assignment.
See [crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for provider-specific effects and failure behavior.

## Dismissing crew

```sh
captain dismiss Jack
```

`dismiss` has no flags. It permanently closes the pane, records the dismissal,
and frees the name for reuse; handle unreported or uncommitted work first. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for dismissal and shared-checkout guardrails.

## Memory

Captain stores session and project graph relationships outside the repository.
`add` accepts `--scope session|project`; `show` accepts `--all` and `--json`;
`prune` accepts `--older-than DAYS`. `query` uses optional Graphify.

```sh
captain memory add "rate limiter" "uses" "per-user windows"
captain memory add "test command" "is" "python -m unittest" --scope project
captain memory show
captain memory query "rate limiter"
captain memory path
captain memory prune --older-than 30
```

The default scope is session; project facts survive into future sessions. See
[memory.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/memory.md)
for paths, identity, retention, Graphify isolation, and pruning safeguards.

## Running commands from another pane

These commands run inside the launched agent's environment. From a separate
Herdr shell in the same project/workspace, pass the session explicitly:

```sh
captain --session <session-id> memory show
captain --session <session-id> crew --task "Check boundary cases"
captain --session <session-id> --agent codex
```

`--session` reuses the captain's graph memory; it starts a fresh native
conversation, not a provider transcript resume. See
[memory.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/memory.md)
for session and project identity.

## Troubleshooting

For install, TTY, crew startup, approval, model confirmation, session,
memory-root, Graphify, and ambiguous-name failures, see
[troubleshooting.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/troubleshooting.md).

```sh
herdr agent read Jack
```

Read the affected pane before retrying or approving anything.

## Command reference

| Command | Purpose |
|---|---|
| `captain [--agent claude\|codex\|pi] [--prompt TEXT] [--no-dashboard]` | Start a captain in this pane |
| `captain crew [NAME] --task TEXT [--agent ...] [--placement pane\|tab] [--direction ...] [--split-pane ...] [--model ...]` | Recruit crew |
| `captain wait NAME [--timeout SECONDS]` | Wait for crew to finish |
| `captain model NAME cheap\|mid\|strong\|<model>` | Switch a running crew's model |
| `captain tell NAME MESSAGE` | Send a follow-up prompt to crew |
| `captain status [--all]` | Print a table of this session's crew |
| `captain dashboard [--interval SECONDS]` | Refresh a crew token-usage table until interrupted |
| `captain focus NAME` | Focus crew's pane and tab |
| `captain session` | Print the current session id |
| `captain init [--global]` | Write a commented `.captain/settings.toml` template |
| `captain dismiss NAME` | Close and retire crew |
| `captain memory add SUBJECT RELATION TARGET [--scope session\|project]` | Save a memory relationship |
| `captain memory query QUESTION` | Search memory with Graphify |
| `captain memory show [--json] [--all]` | Print memory relationships |
| `captain memory path` | Print this session's memory directory |
| `captain memory prune [--older-than DAYS]` | Remove finished sessions' memory |
| `captain --session ID ...` | Run any command against another shell's session |
| `captain --version` | Print the installed version |

All `NAME` arguments are case-insensitive and accept the crew's display
name, ID, or Herdr agent name.

## Development

See [CONTRIBUTING.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/CONTRIBUTING.md)
for setting up a checkout, running checks, and the commit/PR workflow. Current
implementation scope is tracked in
[docs/plan.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/plan.md).

Related CLIs: [Herdr](https://herdr.dev/docs/cli-reference/),
[Graphify](https://graphify.com/docs/cli), and
[Codex's additional instructions](https://learn.chatgpt.com/docs/config-file/config-reference).

## License

Licensed under [MIT](https://github.com/dev-preetamraj/captain-barbossa/blob/main/LICENSE).
