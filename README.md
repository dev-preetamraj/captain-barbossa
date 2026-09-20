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

## Restarting a captain

Find the id with `captain session` before you exit. Then exit the running
agent with `/exit` or Ctrl+D, and rerun:

```sh
captain --session <session-id>
```

Graph memory carries over; the chat transcript does not. You get a fresh
native conversation, not a provider transcript resume.

## Starting a captain

Launch `captain` from an interactive terminal inside a Herdr workspace:

```sh
captain                         # asks: Claude Code or Codex?
captain --agent claude          # Claude Code in this pane
captain --agent codex           # Codex in this pane
captain --agent pi              # pi in this pane
captain --prompt "Inspect this project"
```

It renames the current tab to **Captain Barbossa** and replaces itself with
the chosen native CLI, so native input, history, permissions, and login all
stay with that agent.

Outside a Herdr workspace, `captain` offers to bootstrap one instead of
failing. If Herdr is missing it asks before running Herdr's installer
(`curl -fsSL https://herdr.dev/install.sh | sh`); then it opens a Herdr
workspace at the project, starts captain there with the same `--agent`,
`--session`, and `--prompt`, and attaches your terminal to it. Declining, or
running non-interactively, leaves the old error untouched.

## Recruiting crew

Ask the captain to spin up a crew in plain language; its startup instructions
carry a recruiting ruleset. When you state no preference it recruits with no
questions, using:

- **agent**: the CLI the captain itself runs as
- **placement**: a pane split picked from the tab layout (auto), or a new tab
  when crowded
- **model**: the `cheap` tier, stepped up only for genuinely harder work (see
  below)

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
this tab first, then this session's crew tabs, and splits whichever pane leaves
the largest, squarest halves, preferring panes that hold no crew. The captain's
own pane can be split beside it, never below it, so the captain keeps its full
height. When no split would leave both halves at least 60 columns by 15 rows,
the crew opens in a new tab instead. Explicit `--placement tab` always opens a
new tab; `--split-pane <pane-id>` splits the pane you name instead of searching.
Manual `--direction vertical|horizontal` overrides the automatic direction, and
naming both splits that pane that way whatever its size. The chosen pane,
direction, and a one-line reason are printed and recorded in memory.

**Model tiers.** `--model` takes a provider-neutral tier or free text:

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

**Settings.** Nothing is required: with no settings file anywhere, the
defaults above apply. `<project>/.captain/settings.toml` overrides
`~/.captain/settings.toml`, which overrides the defaults, one key at a time.
`captain init` (or `captain init --global`) writes a template with every
setting commented out at its default, and never overwrites an existing file.
You can retier any provider, set the captain's own model, and turn on the
dashboard pane; see
[docs/settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for the full reference.

New crew panes/tabs open in the same workspace and project without stealing
focus, and the task is submitted once the native agent is ready. A task that
never starts, or an agent waiting for approval, preserves the pane for
inspection and reports an error naming the `herdr agent prompt` command to
send the task by hand; nothing is retried automatically beyond one resend.

Crew share the checkout; see [Editing guardrails](#editing-guardrails) below.

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
```

Blocks until the crew is done, idle, or blocked. It follows the native CLI's
own lifecycle events rather than reading its pane, so a pause between tools is
not mistaken for the end.

The final status and the crew's own report are printed and recorded in memory,
falling back to the crew's last message or the tail of its pane when it
reported nothing. A crew still working when the timeout (900s by default)
expires records nothing and reports an error; wait again, or read its pane
directly with `herdr agent read <name>`.

For **pi** crew the pane tail is written to `tail-<name>.txt` in the session
directory and the wait prints that path instead of the tail itself. pi installs
no lifecycle hooks, so every pi wait falls back to the tail, and the captain
extension steers whatever `wait` prints into the conversation; filing it keeps
each delivery to one line. Read the file when the status and report leave you
unsure. Claude Code and Codex crew, which do have hooks, still print the tail
inline on the rare wait that has no event.

## Sending a follow-up

```sh
captain tell Jack "also update the changelog"
```

Prompts an existing crew in place; its pane, model, and running conversation
are kept. The message replaces the crew's recorded assignment and is saved to
memory, and any idle event left over from before the prompt is consumed first,
so the next `captain wait Jack` reports the new work rather than the old pause.
Dismissed crew are refused; recruit new crew instead.

## Checking crew status

```sh
captain status
captain status --all
```

Prints a plain-text table of this session's crew: name, provider, model,
pane, status, and the first line of their assigned task, truncated to about
60 characters. Status is refreshed live from Herdr for each crew, falling
back to the last recorded status if Herdr can't be reached. Dismissed crew
are omitted unless `--all` is given; `captain status` with no crew prints
"No crew."

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

A `CAPTAIN` row leads the table, then one row per current crew in recruit
order. The captain writes the same lifecycle hook events its crew do, so it
carries usage too; its live status is matched by pane, since the captain has
no agent name of its own. Every frame re-reads the roster, so crew recruited
while it runs appear in the next frame, and any row the dashboard cannot read
degrades to "-" instead of breaking the frame.

- `AGENT` is `provider/model`, with a trailing `-YYYYMMDD` and the provider's
  own prefix dropped (`claude-opus-5` reads `claude/opus-5`). It follows a
  model switched mid-session, not the one recruited with.
- `STATUS` comes from Herdr in one call per frame, like `captain status`.
- `CTX NOW` is the context the last API call actually carried, as a percent of
  the model's window, with a five-cell bar rounded to the nearest fifth. Small
  usage stays visibly small: `.....   7%` is 7%, not an empty reading. It is a
  live reading and falls back to near nothing after `/clear`.
- `CUM TOK` is session-cumulative, all four token kinds summed over every API
  call so far, in `k`/`M`/`B` at three significant digits. It survives `/clear`:
  a crew that starts a new transcript keeps the spend from its earlier one.
- `CUM $` is cumulative USD at list price.
- `$/h 10m` is a rate, not a total: the turns in the trailing 10 minutes,
  extrapolated to an hour. The header names the window because the number alone
  does not, and a rate that never said which minutes it covered would be
  unreadable.

`$0.00` under `$/h 10m` is correct, not a broken column. It means that crew has
spent nothing in the last 10 minutes - it is not burning. That is exactly what
Will's row above shows: `$0.09` cumulative from work it already did, and a zero
rate because it has been quiet longer than the window. `CUM $` never falls; the
rate drops to zero as soon as the window empties, and climbs again on the next
turn.

A zero is always a measured zero. The dashboard never fabricates one: `-` means
the value is unknown - no usage it could read, or turns that carry no timestamp -
and `$?` means the price could not be resolved. So `$0.00` says "nothing", `-`
says "cannot tell", and the two are never interchanged.

`CUM` and `NOW` are different units and do not compare. A single reply to you is
many API calls - one per tool use - and every one of them re-sends the whole
conversation, so the same context is counted again on each call. A captain that
answered once with 21 tool calls on a 68k context had used 7% of a 1M window and
still billed 1.32M cumulative tokens, 96% of them cache reads of that one
context. `CUM TOK` far exceeding `CTX NOW` is the normal case, not a fault:
`CUM TOK` only ever grows, `CTX NOW` rises and falls with the conversation.

Dismissed crew get no row of their own. Their tokens and cost stay inside
`TOTAL` and are disclosed by the annotation under it, which never disappears -
with nobody retired it reads `TOTAL is session-cumulative; USD list est;
rounded`. So the cumulative `TOTAL` columns never shrink when someone is
dismissed, while `TOTAL $/h 10m` counts only the current roster and falls:
retired crew are not burning anything. A total built from partly unknown parts
keeps the known subtotal and marks it: it renders `$3.38+?` and ends its footer
with `+? incomplete`, rather than passing the subtotal off as the whole.

`CUM $` is an estimate at published list prices, not a bill. On a Claude or
ChatGPT subscription it is counterfactual: it says what these tokens would
have cost on the API, which is the only comparable number across providers.

Prices come from LiteLLM's `model_prices_and_context_window.json`, cached for
a day under the state root (`~/.local/state/captain-barbossa/<project>/`) and
refreshed on a background thread, so a frame never blocks on the network. Until
that cache lands the money columns read `$?` rather than a confident `$0.00`.
Point `CAPTAIN_PRICES` at a JSON file of the same shape to override it, for a
negotiated rate or a model LiteLLM does not carry:

```sh
CAPTAIN_PRICES=~/prices.json captain dashboard
```

Context windows prefer the same LiteLLM data and fall back to a small bundled
table of the Claude models Captain launches, so `CTX NOW` still works on a cold
cache or offline. `CAPTAIN_CONTEXT_LIMIT` beats both, for a model neither
knows or to measure against a smaller ceiling than the model's own:

```sh
CAPTAIN_CONTEXT_LIMIT=225000 captain dashboard
```

Codex crew are read from their own rollout log, which states tokens, the model
actually in use, and the context window the CLI enforces, so they carry the
same columns Claude crew do. pi installs no hooks and writes nothing we can
read, so a pi crew shows its name, agent and status with "-" for usage.

The frame fits the pane rather than wrapping. Short panes keep the column
labels, `TOTAL` and its annotation, and fold the crew that do not fit into one
`MORE(N)` row that sums exactly them; make the pane taller to see them
individually. Narrow panes drop the optional header line first, then shorten
model names, then the bar, then the `AGENT` column - the numbers go last.

When a crew pane splits into the captain's own tab, the dashboard is re-nested
directly under the captain; otherwise splitting the captain sideways leaves
the dashboard stretched under both panes. Herdr can only reparent a pane by
way of another tab, so the pane leaves and comes straight back and the tab
flickers once - that is deliberate. The dashboard pane is itself never chosen
as a split target for new crew.

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

This drives the CLI's own `/model` command through Herdr and verifies the
result: without the CLI's own confirmation line naming that model, the command
reports an error and changes nothing. Codex keeps the reasoning level it
already had. A confirmed switch updates the session and memory; the pane, the
conversation, and the assignment are untouched. Claude Code's inline `/model`
also saves the model as the default for new sessions, and the command prints
that as a reminder.

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
    events/<crew>.jsonl           # native hook events, plus cursor files
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
`uv tool install graphifyy` to enable `memory query`, which runs against an
isolated snapshot of project and session memory that is removed afterward.
Relationships can still be added and read with `memory add`/`show` without it.

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
- **Installed from Git before the PyPI release** - switch the install over once
  with `uv tool install --force captain-barbossa`; `uv tool upgrade` then picks
  up each published release.
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
