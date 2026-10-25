# Captain Barbossa

[![CI](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-preetamraj/captain-barbossa/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/captain-barbossa)](https://pypi.org/project/captain-barbossa/)

Captain Barbossa launches a native agent CLI (Claude Code, Codex, pi, Grok) as a
**captain** inside a [Herdr](https://herdr.dev) workspace. The captain recruits
further native agents as **crew** in new Herdr panes or tabs, so a team of
native agent sessions can work on the same project at once. There is no
daemon, custom UI, or tmux layer: everything runs through Herdr, plus a small
graph memory stored outside the repo and a curated repo scope committed with it.

![Captain coordinating crew in separate Herdr panes](docs/assets/captain-barbossa-demo.png)

## Requirements

- macOS or Linux, Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Herdr](https://herdr.dev/docs/cli-reference/), as your terminal workspace
- Claude Code, Codex, pi and/or Grok, installed and already signed in

## Install

```sh
uv tool install captain-barbossa
```

If your shell cannot find `captain` afterward, run `uv tool update-shell` and
restart the terminal.

## Upgrade

```sh
captain update
```

This runs `uv tool upgrade captain-barbossa`. Running captains keep the old
code until restarted with `captain --session <session-id>`.

## Quickstart

Run these in an interactive Herdr workspace:

```sh
captain --agent codex
# Ask the captain: "Review the current changes." (read-only: runs as a background crew)
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

`captain --rename NAME` names the current session (`--session ID` targets another).
An unnamed session is titled by the captain's own model: while it has no name,
its instructions ask for a 2-5 word title of the first task via
`captain --rename TITLE --if-unnamed`, run alongside its first command. That flag
never replaces a name, so your rename always wins; a session not titled yet,
including one from before titles, shows `-` until its next launch titles it.
`captain --resume` opens a searchable picker of this project's sessions (title, last
activity, crew; no id) and resumes the choice from that project root, like
`captain --session ID`; without a terminal it prints the plain list, or reports none.
`captain --resume NAME|ID` skips the picker; a name shared by several sessions is
an error, so use the id. Neither flag combines with a subcommand.

## Pane-free launch plans

`captain launch-plan --agent pi --model MODEL [--effort LEVEL] [--session ID]`
returns one versioned JSON object for a desktop host to start `pi --mode rpc`
over pipes. It needs no TTY or Herdr pane and does not start the CLI. Only pi RPC
is supported; missing choices and failures return structured errors and a nonzero
exit status. Agent/model settings may supply new-chat choices; no picker opens.

Preparation may create Captain session and extension files outside the checkout.
The `env` field contains overrides only (`null` means unset), never inherited
credentials. The host applies these to its own environment. `--session ID`
requires the exact existing pi session file recorded by the captain extension;
unlike terminal restarts, it resumes that native chat or fails, never starts a
fresh one. An omitted model on resume preserves pi's saved model. This does not
make Herdr-backed crew recruitment pane-free.

## Starting a captain

Launch `captain` from an interactive terminal inside a Herdr workspace:

```sh
captain                         # asks: Claude Code, Codex, pi, Grok?
captain --agent claude          # Claude Code in this pane
captain --agent codex           # Codex in this pane
captain --agent pi              # pi in this pane
captain --agent grok            # Grok in this pane
captain --prompt "Inspect this project"
```

`--agent claude|codex|pi|grok` selects the native CLI, `--prompt TEXT` supplies its
first task, `--session ID` resumes Captain state, `--resume [NAME|ID]` picks a
session to resume, `--rename NAME` names one, and `--no-dashboard` skips an
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
captain crew Gibbs --task "Review the current changes" --agent claude --model mid \
  --placement pane --split-pane auto --direction auto
```

Omitted flags resolve in code: `[crew] agent` is empty as shipped and follows the
captain's CLI, model uses the configured crew tier (`cheap` as shipped), and
`[crew] placement = "auto"` selects the next slot and reports `defaults.toml` as
its source. Explicit flags win; split flags imply a pane. Recruitment prints each
choice's source. Only an empty crew agent with no captain metadata needs a picker
(or explicit `--agent` in a non-TTY). Only the captain recruits; crew forward
delegation requests to it. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for naming, launch behavior, checkout modes, failure handling, and editing guardrails.

Crew use the captain working tree by default, including its dirty, staged, and
untracked files. Give simultaneous shared-checkout writers disjoint files and
serialize edits to the same file. When `[crew] isolation_enabled = true`,
`--checkout auto|shared|worktree` selects shared work or a detached Git worktree;
`auto` isolates writable recruits with no owned paths or overlapping ownership.
Isolated results include only owned paths. Review them with `captain result show`,
then use `result apply` to copy them into the captain checkout unstaged or
`result discard --reason TEXT`; retained checkouts appear under `captain worktree`.

Use `--profile NAME` to select `[profiles.NAME]` from global/project settings,
bundling agent, model, execution (`foreground` or `background`), checkout, and
placement. Flags override profile keys; omitted keys retain existing defaults.
`--foreground` overrides background execution. Profiles
print and persist their effective choices and sources, and never grant assignment
permissions. See
[settings.md](docs/settings.md#explicit-recruitment-profiles) for examples and restrictions.

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

| Tier     | Claude Code      | Codex                | Grok                |
|----------|------------------|----------------------|---------------------|
| `cheap`  | claude-haiku-5-5  | gpt-5.6-luna         | grok-4.7-build-fast  |
| `mid`    | claude-sonnet-5-5 | gpt-5.6-sol          | grok-4.6             |
| `strong` | claude-opus-5-5   | gpt-6-astra          | grok-4.7             |

**`cheap` is the configured default** when `--model` is omitted, rather than
falling through to whatever the native CLI uses. When recruiting, the captain
chooses a tier by task complexity: `cheap` for a focused fix or established
pattern, `mid` for a normal feature or change inside one area, and `strong` for
design, debugging, or multi-file and long-context work. Risk or importance
alone does not call for a stronger model.

Retier a running crew with `captain model <name> cheap|mid|strong` when its
model stops fitting the work, such as a cheap crew that is stuck or a
mechanical follow-up on a strong crew.

Free text also works and is matched to the closest model the chosen CLI
offers (exact IDs and aliases first, then prefixes, substrings, and close
spellings): Claude Code additionally offers `claude-fable-5-1` (fable);
Codex additionally offers `gpt-5.6-terra` (terra), `gpt-6-luna` (luna),
`gpt-6-sol`, and `gpt-6.1-sol` (sol); Grok additionally
offers `grok-4.5`. Ambiguous or unknown text reports the options and creates nothing.

```sh
captain crew --task "Debug the failure" --model strong
```

See [settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for retiering providers and setting model defaults.

## Configuring defaults

Nothing is required. Project settings override global settings one key at a
time, and CLI flags win for one invocation. `init` writes a fully commented
template for a new file; existing files are left untouched.

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
captain wait Jack --json --ack <delivery-id>
```

New crew use explicit assignments: `wait` reports activity, questions, approval
prompts, or completion; native idle alone does not mean the assignment is done.
Claude PermissionRequest hooks validate assignment grants: known scoped requests
are allowed once, ungranted actions are denied, and uncertain or destructive
requests remain at the native user prompt. No persistent permission is granted.
Notifications repeat until acknowledged with `--ack`; `--json` returns a stable
envelope with status, delivery ID, crew, assignment ID, and summary.
`--timeout SECONDS` overrides the 86400-second default. It follows native
lifecycle events, with pane-tail fallbacks documented in
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md).

## Sending a follow-up

```sh
captain tell Jack "also update the changelog" --assignment <assignment-id>
```

`tell` appends to an active assignment while keeping the pane, model, and
conversation; it does not replace the original task. New assignments declare
owned paths with repeated `--owns PATH` (absolute paths outside the checkout are
allowed) and grants with repeated `--allow ACTION`.
Use `ask`/`answer` for a question, `done --report` for completion, and
`assign --handoff` to reuse a finished assignment's crew. Dismissed crew are refused.

`assign`, `tell`, and `answer` write a durable mail file. For Claude crew,
every ring is the fixed wake line; native hooks deliver the body as
`additionalContext` and write its receipt after flushing it. For other providers,
the first ring prompts the assignment plus a line to run `captain inbox NAME`;
a landed body already counts as delivered, and a later ring is the inbox line alone.
`inbox` remains a manual re-read and receipt writer. `done` receipts landed mail;
undelivered mail is served in its refusal.
Newly launched crew may omit `--assignment` on `ask`, `done`, and `check`: their
launch-bound `CAPTAIN_ASSIGNMENT` is validated against crew identity, incarnation,
and the active assignment. Missing or stale context fails; captain calls and
replacement assignments still require explicit IDs. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for assignment and event-cursor behavior.

## Checking crew status

```sh
captain status
captain status --all
```

`status` prints each crew's name, provider, model, pane, task, and evidence-based
`state:… source:… detail`. Missing evidence stays `unknown`; native idle is not done.
`--all` includes dismissed crew. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for fallbacks and output details.

## Watching crew token usage

```sh
captain dashboard
captain dashboard --interval 5
captain dashboard --refresh-prices
```

`captain dashboard` refreshes a plain-text table of the session's crew in the
current pane every 2 seconds by default (`--interval SECONDS` to change that).
Usage reads use cached prices; only `--refresh-prices` opts into a background
network refresh when the cache is missing or stale.

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

`dismiss` permanently closes the pane, records the dismissal, and permits name
reuse with an explicit handoff; handle unreported or uncommitted work first.
`--confirm-unlanded` proceeds only when the user accepts Git-reported unlanded work.
Unread mail is bounced, not delivered, and the command reports which messages
bounced. After a Herdr workspace disappears, `captain --session ID dismiss Jack`
from a new workspace releases that session's crew once Herdr confirms the old
workspace gone: no done report is needed, no pane is closed, unlanded work is
still refused, and a managed writer stays `cleanup_pending` until its shutdown is
confirmed. See
[crew-lifecycle.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/crew-lifecycle.md)
for dismissal and shared-checkout guardrails.

## Inspecting files, state, and Git

```sh
captain inspect files src
captain inspect read README.md
captain inspect search "literal text" src
captain inspect state repo graph.json
captain inspect git diff --staged
```

`inspect` runs without Herdr lookup. Paths are literal and project-bounded,
except that `files`, `read` and `search` accept an absolute path outside the
checkout (crew: never captain state);
`state` selects only `session`, `project`, or `repo` storage (crew: repo only).
Git operations are fixed: `status`, `log`, `current-branch`, `root`, `diff`,
`branches`, `ls-files`, and `grep --text TEXT`.
No shell, arbitrary flags, or external helpers are accepted. Results are JSON,
bounded to 64 KiB, 200 results, 10,000 entries, 8 MiB searched, and five seconds;
limits produce truncation markers or explicit errors. Filesystem deadlines are
cooperative. Git requires a trusted system installation and an ordinary stable
local checkout; linked worktrees, partial clones, alternates, and unsupported
config are refused. Git must be root-owned at `/usr/bin/git` on Linux or the
Command Line Tools installation on macOS; the macOS `/usr/bin/git` launcher is
not used. Reads accept regular UTF-8 text files; enumeration skips symlinks and
`.git`, and direct symlink reads fail. Native sandbox permissions still apply. See
[crew-lifecycle.md](docs/crew-lifecycle.md) for protocol and safety limits.

## Running work without a crew pane

`captain do` runs synchronous, deterministic work without recruiting crew:
`commit --message TEXT [PATH ...]` stages named paths, refuses staged entries
outside that scope or files mixing staged and unstaged changes, then commits the
verified index (omit paths to commit the existing index); `push` pushes the current
branch and sets its upstream on the first push; `branch NAME` creates and
switches to a branch; `switch NAME` switches to an existing branch; `fetch`
fetches from the remote; `pull` is fast-forward only.

`captain do run TARGET` runs a literal target declared in the root Makefile.
Outward-facing, destructive and history-rewriting actions require explicit user
authorization; a concrete request already authorizes its stated scope. Declaring
a target grants no permission. Git verbs still expose no force or rewriting flags.

For an operation request such as 'push the code', the captain runs `captain do push`
directly within that authorization, never through crew, quiet, a model, or a resolver.
Missing arguments or authorization are requested before execution.

`captain quiet` runs one headless model turn using the session captain's native
CLI, prints its report, and needs no crew assignment or wait. It is read-only
by default; repeat `--write PATH` to name files it may edit, leaving edits
unstaged, or `--read DIR` to let a read-only turn read outside the checkout. `--diff worktree|staged` appends the selected Git diff to the task;
`--model` selects a tier or model (the configured crew tier by default), and
`--timeout SECONDS` overrides the 900-second limit.

```sh
captain do run test
# Explicit synchronous opt-in; the captain defaults read-only work to a background crew:
captain quiet --task "Review the current changes" --diff worktree
captain quiet --task "Correct the setup instructions" --write README.md
```

Execution commands record runs in the session's `events/captain.jsonl` so work
without a pane can be reviewed afterward. They are captain-only commands.

### Crew session cleanup

Cleanup freezes usage and considers only native session identities recorded for
that crew incarnation. It never targets the captain's history or unrelated native
sessions. Missing hooks/manifests, unobserved or ambiguous identities, partial provider
coverage, unknown writers, and shared-history/sidecar uncertainty remain reported as
pending, partial, or unsupported, with no broad deletion. Retained non-complete
native-history journals block session pruning. Authoritative lifetime checks and the
usage freeze happen before any targeted native deletion.

### Background crew

`captain crew --background --agent codex|pi --task TEXT [NAME] [--model ...]` runs
the same read-only headless turn as `quiet`, without blocking the captain: a crew
with a name, assignment and report, but no pane. The captain defaults all delegated
read-only work (research, exploration, review, code questions), even one bounded turn, to it;
synchronous `quiet` or a visible read-only crew only when you ask, and writable work
stays foreground. A requested provider that cannot run it (claude, grok) is never silently
switched. Its report or failure arrives
through `wait` like any crew's, and a pi captain watches it automatically. pi takes a
steered message in only once its current turn ends, so a delivery can reach its history
late; the watch warns after 30 seconds and keeps checking history until it expires,
acknowledging the delivery only once it is there and never resending it.

- Opt-in only; plain `captain crew` is unchanged. Placement flags, `--owns`,
  `--handoff`, any `--allow` beyond `read`/`search`, a non-finite timeout, and the
  `claude` and `grok` agents are refused before anything is reserved.
- Only codex and pi run in the background, because only their read-only gate holds
  against the user's own configuration: codex ignores the user's config under its
  OS read-only sandbox, and pi adds `--no-extensions --no-mcp --exclude-tools
  mcp__* --no-skills --no-prompt-templates --no-approve`. A headless claude turn
  still runs the user's hooks and honours their settings' allow rules, and grok
  documents no flag to drop its MCP servers and plugins. `quiet` is unchanged for
  all four.
- A detached owner process runs the native CLI under a guard that leads its own
  process group, bounded by `[crew] background_timeout` (1800 seconds by default)
  and 8 MiB of output. When the run ends, for any reason, the whole group is
  killed. If the owner itself dies, at any point, the guard kills the group. That
  contains every process that stays in the group; one that leaves it (`setsid`,
  daemonizing, its own process group) is outside it and can outlive the run, and
  the owner stops waiting for output it holds after 5 seconds. The owner records exactly one outcome:
  `done` with the report, or an `error` for a failure, timeout, oversize output or
  cancellation. An exit status never counts as success; only a complete result
  document does.
- `captain interrupt NAME` asks the owner to stop the run and reports whether the
  cancel took effect or completion won; only the owner signals the native CLI. If the
  owner is gone, `status` shows `state:failed` with `[background-lost]` and the next
  `wait` records and delivers it; nothing is restarted.
- `tell`, `answer`, `focus` and `model` are refused: there is no conversation to
  continue. `dismiss` works once the outcome is acknowledged.
- The run continues when the captain exits; its outcome is delivered when the session
  is reopened with `captain --session ID`. `wait` and `interrupt` with `--session` need
  no Herdr pane when every crew they reach is a background crew. A session started by
  an older captain in temporary storage refuses `--background`.

Background execution remains one read-only headless turn in its selected checkout;
provider-specific read-only controls still apply. There is no PTY, persistent daemon,
worker pool, or automatic commit.

```sh
captain crew --background --agent codex --task "Find why startup is slow; report the cause"
captain status
captain interrupt Jack --reason "no longer needed"
```

## Memory

Captain stores session and project graph relationships outside the repository,
and a third `repo` scope committed with the code as `.captain/graph.json`.
`add` accepts `--scope session|project|repo` plus `--because` and `--supersede`
for repo scope; `show` accepts `--scope`, `--all`, and `--json`; `path` accepts
`--scope session|project|repo`; `prune` accepts
`--older-than DAYS`. `query` performs case-insensitive literal substring search over stored
subjects, relations, and objects in all three scopes; no external tool is needed.
`show` and `path` do not create, migrate, lock, or rewrite memory; scoped JSON
reads only the selected scope.

```sh
captain memory add "rate limiter" "uses" "per-user windows"
captain memory add "test command" "is" "python -m unittest" --scope project
captain memory add "placement" decided "declared tab shapes" --scope repo \
  --because "even ratios must survive a dismissal"
captain memory init --apply
captain memory show
captain memory show --scope repo
captain memory query "rate limiter"
captain memory path
captain memory prune --older-than 30
```

The default scope is session; project facts survive into future sessions. New
sessions live in the durable state root, so a reopened session still holds its
assignments and undelivered outcomes; one created by an older captain in OS temp
storage is opened where it is. The durable project namespace is keyed by the captain
checkout. Crew share that session/project memory. Repo
facts are shared with the team through version control, so the code fixes their
shape: an explicit `--scope repo` only, a relation from the closed set
`decided|method|convention`, a required `--because` rationale, deterministic
sorted bytes that re-add as a no-op, and `--supersede` to change one rather than
newest-wins. Nothing is committed for you.

`captain memory init` seeds that graph from the rules the project already
states, the counterpart of `captain init` writing `.captain/settings.toml`. It
reads `AGENTS.md`, else `CLAUDE.md` (`--from PATH` overrides), turns each bullet
under a `Rules`/`Key facts` section into one `convention` fact, and previews
them until you pass `--apply`. Re-running adds only what is new. See
[memory.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/memory.md)
for paths, identity, retention, literal search, and pruning safeguards.

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
memory-root and ambiguous-name failures, see
[troubleshooting.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/troubleshooting.md).

```sh
herdr agent read Jack
```

Read the affected pane before retrying or approving anything.

## Command reference

| Command | Purpose |
|---|---|
| `captain [--agent claude\|codex\|pi\|grok] [--prompt TEXT] [--no-dashboard]` | Start a captain in this pane |
| `captain crew [NAME] --task TEXT [--profile NAME] [--checkout auto\|shared\|worktree] [--owns PATH] [--allow ACTION] [--handoff ID] [--agent ...] [--placement pane\|tab] [--direction ...] [--split-pane ...] [--model ...]` | Recruit crew |
| `captain crew [NAME] --background --agent codex\|pi --task TEXT [--model ...]` | One read-only headless turn as a paneless crew |
| `captain interrupt NAME [--reason TEXT]` | Stop a crew's current turn; cancels a background run |
| `captain wait NAME [--timeout SECONDS] [--json] [--ack DELIVERY_ID]` | Read and acknowledge a crew notification |
| `captain wait --all [--timeout SECONDS] [--json]` | Read all crew's pending deliveries; acknowledge per name/ID |
| `captain recover` | JSON recovery digest (title, reports, memory, assignments) with explicit omission counts; no acknowledgements. Launch, `--resume` and `--session` already inject it; use after compaction |
| `captain model NAME cheap\|mid\|strong\|<model>` | Switch a running crew's model |
| `captain models [--agent claude\|codex\|pi\|grok]` | List model IDs, aliases, and tier resolutions |
| `captain tell NAME MESSAGE [--assignment ID]` | Append a follow-up to an active assignment |
| `captain inbox NAME` | Print a crew's queued mail oldest first and write the read receipt |
| `captain assign NAME --task TEXT --handoff ID [--owns PATH] [--allow ACTION]` | Start the next assignment after acknowledged completion |
| `captain ask NAME QUESTION [--assignment ID]` | Record one pending question; ID may default for newly launched crew |
| `captain answer NAME QUESTION_ID MESSAGE --assignment ID` | Answer that question |
| `captain done NAME [--assignment ID] --report TEXT` | Record explicit completion; ID may default for newly launched crew |
| `captain check NAME ACTION [PATH ...] [--assignment ID]` | Check grants; ID may default for newly launched crew; executes nothing |
| `captain inspect files\|read\|search\|state\|git ...` | Bounded local inspection |
| `captain do commit\|push\|branch\|switch\|fetch\|pull\|run ...` | Run deterministic work without crew |
| `captain quiet --task TEXT [--diff worktree\|staged] [--write PATH] [--read DIR] [--model MODEL] [--timeout SECONDS]` | One headless model turn; repeat `--write` for multiple files |
| `captain status [--all]` | Print a table of this session's crew |
| `captain dashboard [--interval SECONDS] [--refresh-prices] [--once]` | Refresh a crew token-usage table, or print one frame |
| `captain focus NAME` | Focus crew's pane and tab |
| `captain session` | Print the current session id |
| `captain --rename NAME [--if-unnamed]` | Name the current session (the captain titles unnamed ones) |
| `captain --resume [NAME\|ID]` | List current-project sessions, or resume one from its project |
| `captain update` | Upgrade the installed tool |
| `captain init [--global]` | Write a commented `.captain/settings.toml` template |
| `captain dismiss NAME [--confirm-unlanded]` | Close and retire crew; confirmation permits known unlanded shared work |
| `captain result collect WORKTREE_ID` | Snapshot an isolated crew's owned paths |
| `captain result show\|apply RESULT_ID` | Review or apply an isolated result unstaged |
| `captain result discard RESULT_ID --reason TEXT` | Explicitly discard an isolated result |
| `captain worktree list` | List retained isolated checkouts |
| `captain worktree cleanup WORKTREE_ID` | Clean up a retained isolated checkout |
| `captain memory add SUBJECT RELATION TARGET [--scope session\|project\|repo] [--because WHY] [--supersede]` | Save a memory relationship |
| `captain memory init [--from PATH] [--apply]` | Seed repo memory from the project rulebook |
| `captain memory query QUESTION` | Literal search of stored memory |
| `captain memory show [--scope SCOPE] [--json] [--all]` | Print memory relationships |
| `captain memory path [--scope session\|project\|repo]` | Print a scope's directory without creating it |
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

Related CLIs: [Herdr](https://herdr.dev/docs/cli-reference/) and
[Codex's additional instructions](https://learn.chatgpt.com/docs/config-file/config-reference).

## License

Licensed under [MIT](https://github.com/dev-preetamraj/captain-barbossa/blob/main/LICENSE).
