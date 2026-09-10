# Captain Barbossa

[![CI](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml)

Captain Barbossa launches a native agent CLI (Claude Code or Codex) as a
**captain** inside a [Herdr](https://herdr.dev) workspace. The captain recruits
further native agents as **crew** in new Herdr panes or tabs, so a team of
native agent sessions can work on the same checkout at once. There is no
daemon, custom UI, or tmux layer: everything runs through Herdr, plus a small
graph memory stored outside the repo.

## Requirements

- macOS or Linux, Python 3.11+
- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Herdr](https://herdr.dev/docs/cli-reference/), as your terminal workspace
- Claude Code and/or Codex, installed and already signed in

## Install

No manual clone is needed; uv downloads the package and installs its
dependencies in an isolated environment.

```sh
uv tool install git+https://github.com/dev-preetamraj/captain-barbossa.git
```

If your shell cannot find `captain` afterward, run `uv tool update-shell` and
restart the terminal.

## Upgrade

```sh
uv tool upgrade captain-barbossa
```

`captain --version` prints the installed version. uv re-resolves the Git
source recorded at install time and installs the latest commit on the default
branch, even when the version number has not changed.
`uv tool install --force git+https://github.com/dev-preetamraj/captain-barbossa.git`
does the same. Running captains keep the old code until restarted with
`captain --session <session-id>`.

## Starting a captain

Launch `captain` from an interactive terminal inside a Herdr workspace:

```sh
captain                         # asks: Claude Code or Codex?
captain --agent claude          # Claude Code in this pane
captain --agent codex           # Codex in this pane
captain --prompt "Inspect this project"
```

It renames the current tab to **Captain Barbossa** and replaces itself with
the chosen native CLI, so native input, history, permissions, and login all
stay with that agent.

## Recruiting crew

Ask the captain to spin up a crew in plain language; its startup instructions
carry a recruiting ruleset. When you state no preference it recruits with no
questions, using:

- **agent**: the CLI the captain itself runs as
- **placement**: a pane split picked from the tab layout (auto), or a new tab
  when crowded
- **model**: a tier picked from the task (see below)

Every choice you do state is used as given; the captain asks at most one
question, and only when you hand a choice back to it ("ask me where to put
it") or name one too vaguely to map to a flag. Only the captain recruits;
crew forward any delegation request back to the captain instead of spawning
their own.

You can also run the command directly from a shell attached to the captain's
session:

```sh
captain crew --task "Review the current changes"
captain crew gibbs --task "Review the current changes"   # request a specific name
```

**Names.** Every crew gets a one-word Pirates of the Caribbean name: Jack,
Will, Elizabeth, Gibbs, Anamaria, Pintel, Ragetti, Cotton, Marty, Tia, Davy, or
Sao, assigned in that order and skipping names already in use. Dismissing
crew frees the name for reuse, so the next recruit takes the lowest free
roster name again. Once every name is taken, numbering starts at Jack-2.
Barbossa is reserved for the captain. The same name is used everywhere: the
crew ID, the pane/tab label, and the name in memory, `wait`, `focus`,
`model`, and `dismiss`.

**Placement.** `--placement pane|tab`, and for a pane, `--direction
vertical|horizontal|auto` with `--split-pane <pane-id>|auto`. `auto` searches
existing crew tabs (current tab first) for the split that leaves both halves
largest and squarest on screen, at least 60 columns by 15 rows. Ties favor
panes without crew, then panes nearest the captain; splitting the captain's
own pane ranks last. When nothing fits, the crew opens in a new tab instead.
The chosen pane, direction, and a one-line reason are printed and recorded in
memory.

**Model tiers.** `--model` takes a provider-neutral tier or free text:

| Tier     | Claude Code       | Codex                |
|----------|-------------------|-----------------------|
| `cheap`  | claude-haiku-4-5  | gpt-5.3-codex-spark   |
| `mid`    | claude-sonnet-5   | gpt-5.6-terra         |
| `strong` | claude-opus-5     | gpt-6-astra           |

The captain picks a tier from the task: `cheap` for mechanical edits,
renames, formatting, and docs; `mid` for normal features, tests, and work
inside one area; `strong` for design, debugging, multi-file changes, or
long-context reads. Free text also works and is matched to the closest model
the chosen CLI offers (exact IDs and aliases first, then prefixes,
substrings, and close spellings): Claude Code additionally offers
`claude-fable-5-1` (fable); Codex additionally offers `gpt-5.4-mini` (mini),
`gpt-5.6-luna` (luna), `gpt-5.6-sol` (sol), and `gpt-5.5`. Ambiguous or
unknown text reports the options and creates nothing. Without `--model`, the
CLI's own default applies.

New crew panes/tabs open in the same workspace and project without stealing
focus. Captain waits for the native agent to hold an idle state across
consecutive polls before naming it and submitting its task, since a freshly
drawn TUI silently drops a submitted prompt. A task that never starts, or an
agent waiting for approval, preserves the pane for inspection and reports an
error naming the `herdr agent prompt` command to send the task by hand;
nothing is retried automatically beyond one resend.

Crew share the checkout; see [Editing guardrails](#editing-guardrails) below.

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
```

Polls Herdr until the crew settles at idle (across consecutive polls, so a
pause between tools isn't mistaken for the end), or reports done or blocked.
Records a `completed` entry in memory with the final status and the crew's
own report, falling back to the tail of its pane when it recorded none, and
prints the same. A crew still working when the timeout (900s by default)
expires records nothing and reports an error; wait again, or read its pane
directly with `herdr agent read <name>`.

## Focusing crew

Tell the captain "focus on Jack", "switch to Will", or "take me to
Elizabeth", or run the command directly:

```sh
captain focus Jack
```

Names are case-insensitive; crew IDs and Herdr agent names also work.
Focusing switches to the crew's tab first when it differs from the captain's,
follows the registered agent if its pane has moved, and never sends input or
interrupts its work. An unknown or ambiguous name reports the available
choices.

## Switching a running crew's model

Ask the captain to step a crew up or down a tier when its model stops fitting
the work, or run the command directly:

```sh
captain model Jack strong
captain model Will cheap
```

This drives the CLI's own `/model` command through Herdr: Claude Code takes
the model inline; Codex opens its numbered picker, reads the pane for the
matching row, and keeps the reasoning level it already had. Either way the
switch is verified from the pane; without the CLI's own confirmation line
naming that model, the command reports an error and changes nothing. A
confirmed switch updates the session and memory; the pane, the conversation,
and the assignment are untouched. Claude Code's inline `/model` also saves
the model as the default for new sessions, and the command prints that as a
reminder.

## Dismissing crew

```sh
captain dismiss Jack
```

Closes the crew's pane, retires the name (freeing it for reuse), and records
the dismissal in memory. This is permanent, so confirm any unreported or
uncommitted work is handled first: crew commit their own hunks, and the
captain only cleans up the user's leftover edits afterward.

## Editing guardrails

Crew share one checkout, so both captain and crew instructions carry the
same contract:

- Edit only files in your own assignment; give simultaneous writers disjoint
  files and serialize same-file work.
- Re-read a file right before editing it, and keep others' unexpected
  changes in place.
- Stage and commit only your own files/hunks, never `git add -A` or
  repo-wide formatting.
- Never overwrite, rewrite from scratch, or discard existing or uncommitted
  work; edit in place, and ask the user first if an assignment implies
  replacing content.
- Finish or record a handoff before anyone else edits your file.
- Never commit or bump the version unless the user explicitly asks;
  otherwise leave the work in the working tree and report the diff.

Nothing locks files: this is an instruction-only contract, not enforcement.

## Memory

Captain stores graph relationships and launch metadata outside the
repository, split so durable project facts survive OS temp cleanup while
ephemeral session state does not:

```text
~/.local/state/captain-barbossa/<hash of project path>/
  graph.json                      # explicit --scope project facts

<OS temp>/captain-barbossa-<uid>/<hash of project path>/
  sessions/<session-id>/
    session.json                  # workspace and crew references
    graph.json                    # this session's memory only
```

`$XDG_STATE_HOME` is honored in place of `~/.local/state` when set. Git
repositories use their checkout root as project identity; other directories
use the launch directory. Crew inherit their captain's project and session;
only explicitly saved project facts carry into other sessions. Set
`CAPTAIN_MEMORY_ROOT` before starting captain to redirect both roots at once
(used for test isolation and custom retention); it must point outside the
project.

```sh
captain memory add "rate limiter" "uses" "per-user windows"
captain memory add "test command" "is" "python -m unittest" --scope project
captain memory show
captain memory query "rate limiter"
captain memory path
```

`memory show` prints the most recent relationships as `[scope] [subject,
relation, object]`; `--all` shows every link, `--json` dumps the raw graph.
Default scope is `session`; use `--scope project` only for facts that should
survive into future sessions.

[Graphify](https://graphify.com/docs/cli) is optional: install it with
`uv tool install graphifyy` to enable `memory query`, which runs `graphify
query` against an isolated snapshot of project and session memory (output,
cache, and query logging are scoped to that snapshot and removed afterward).
Relationships can still be added and read with `memory add`/`show` without
it.

Session directories accumulate as sessions end. `captain memory prune` (also
run automatically, silently, and best-effort at every launch) removes
directories where nothing has been touched for `--older-than` days (7 by
default) and Herdr reports no live agent in their panes; when Herdr is
unreachable, only directories twice that age are removed. The current
session and the durable project graph are never removed.

```sh
captain memory prune
captain memory prune --older-than 30
```

## Running commands from another pane

These commands run inside the launched agent's environment. From a separate
Herdr shell in the same project/workspace, pass the session explicitly:

```sh
captain --session <session-id> memory show
captain --session <session-id> crew --task "Check boundary cases"
captain --session <session-id> --agent codex
```

`--session` reuses the captain's graph memory; it starts a fresh native
conversation, not a provider transcript resume.

## Troubleshooting

- **`captain: command not found`** - run `uv tool update-shell` and restart
  the terminal.
- **"Launch captain from an interactive Herdr terminal."** - `captain` with
  no subcommand needs a TTY; run it directly in a Herdr pane, not through a
  script or pipe.
- **Crew pane opens but the task never starts, or is marked
  `needs_attention`** - the native CLI may be waiting for approval or
  sign-in. Inspect the pane in Herdr; the task is not retried automatically
  past one resend. Send it by hand with
  `herdr agent prompt <agent-name> '<task>'`.
- **"... is waiting for input or approval instead of starting the task."** -
  read the pane before approving, then send the requested key with
  `herdr agent send-keys <name> <key>` (Claude Code may need Enter or a
  number, not always `y`).
- **"... did not confirm the switch to ..."** - the native CLI didn't echo
  the expected model name; read the pane with `herdr agent read <name>`
  before retrying `captain model`.
- **"This session belongs to another project or Herdr workspace."** - a
  session ID is tied to the project and workspace it was created in; start a
  new captain or pass the matching `--session`.
- **"durable memory root ... is inside the OS temp directory"** - a captain
  started before the state/temp split is still exporting one root to its
  children; restart it so project-scope memory survives temp cleanup.
- **`memory query` fails or reports a skip** - Graphify isn't installed; run
  `uv tool install graphifyy`, or use `memory show`/`add` instead.
- **Ambiguous crew name or model** - the error lists the available crew or
  models; ask for one of those exactly.

## Command reference

| Command | Purpose |
|---|---|
| `captain [--agent claude\|codex] [--prompt TEXT]` | Start a captain in this pane |
| `captain crew [NAME] --task TEXT [--agent ...] [--placement pane\|tab] [--direction ...] [--split-pane ...] [--model ...]` | Recruit crew |
| `captain wait NAME [--timeout SECONDS]` | Wait for crew to finish |
| `captain model NAME cheap\|mid\|strong\|<model>` | Switch a running crew's model |
| `captain focus NAME` | Focus crew's pane and tab |
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for setting up a checkout, running
checks, and the commit/PR workflow. Current implementation scope is tracked
in [docs/plan.md](docs/plan.md).

Related CLIs: [Herdr](https://herdr.dev/docs/cli-reference/),
[Graphify](https://graphify.com/docs/cli), and
[Codex's additional instructions](https://learn.chatgpt.com/docs/config-file/config-reference).

## License

Licensed under [MIT](LICENSE).
