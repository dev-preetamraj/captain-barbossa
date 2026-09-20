# Market research: where Captain Barbossa stands and what to build next

Date: 2026-09-19. Researched against the code in `src/captain_barbossa/` at
`db96a8c` (v0.17.1), so every gap below is checked against what is actually
implemented, not against the README's aspirations.

**Fixed premises (not under review).** Herdr-native, captain is a real native
CLI, crew in real panes, one shared checkout, hook-driven state, graph memory
outside the repo, no daemon or custom UI. Audience is experienced professional
developers who want control and auditability, not autonomy.

---

## 1. Competitor table

| Tool | Isolation | State detection | Review surface | Cost visibility | Agents | Control surface |
|---|---|---|---|---|---|---|
| **Captain Barbossa** | Shared checkout, instruction-only contract | Native hooks (Claude `--settings`, Codex `notify`) with pane fallback | None; crew self-report prose | None; `cheap` model tier is the only lever | Claude, Codex, pi | Herdr panes + `captain` CLI |
| **[Claude Code agent teams](https://code.claude.com/docs/en/agent-teams)** | Shared checkout, warns "two teammates editing the same file leads to overwrites" | First-class: mailbox, `TeammateIdle`/`TaskCreated`/`TaskCompleted` hooks, shared task list with file-locked claiming | In-terminal agent panel, per-teammate transcripts | `/usage` per session; docs quantify teams at ~7x a single session | Claude only | In-process panel or tmux/iTerm2 split panes |
| **[Claude Squad](https://github.com/smtg-ai/claude-squad)** (8.5k★) | Git worktree per task | tmux session per agent, TUI list | Built-in diff review before apply; checkout/commit/push | Not documented | Claude, Codex, Gemini, Aider | TUI over tmux |
| **[NTM](https://github.com/Dicklesworthstone/ntm)** | Worktrees **plus file reservations** via Agent Mail | Pane identity badges, drift detection, dashboard, SSE/WebSocket, robot JSON | Triage/impact analysis via Beads tracker | Seat selection (CAAM) and provider quota checks | Claude, Codex, Gemini | TUI palette + REST/OpenAPI |
| **[dmux](https://github.com/standardagents/dmux)** | Worktree per pane, automatic branch management | tmux pane manager, durable terminals | Branch per pane | No | Claude, Codex, opencode | tmux |
| **[nudge](https://github.com/cottrell/nudge)** | tmux swarm, config-driven | Activity monitor (working/idle), durable log messaging delivered **only when idle** | No | Best-effort quota tracking | Multiple | tmux + config |
| **[Conductor](https://conductor.build/)** | Worktree per workspace | Desktop app, at-a-glance status | Strong diff viewer and merge/PR flow (its core pitch) | Not advertised | Claude, Codex, Cursor | macOS GUI |
| **vibe-kanban** | Worktree per card | Kanban board columns | Board + browser preview | No | Multiple | Web board; now community-maintained after Bloop shut down ([roundup](https://www.augmentcode.com/tools/open-source-agent-orchestrators), [comparison](https://abralo.com/alternatives)) |
| **[Tmux-Orchestrator](https://github.com/Jedward23/Tmux-Orchestrator)** | Shared, none | Self-scheduled check-ins (`schedule_with_note.sh`) | None | None | Claude | tmux hierarchy (orchestrator → PM → engineer) |

**Where Barbossa is genuinely differentiated:** it is the only tool in the set
that defaults crew to a *cheap* model tier and can retier a running agent in
place, the only one whose state signal is the vendor's own lifecycle hooks
rather than screen-scraping or a wrapper process, and the only one that keeps a
durable cross-session graph memory outside the repo. Those are real moats for
the stated audience.

**Where the whole category has settled and Barbossa has not:** every serious
competitor ships either worktree isolation or an explicit file-reservation
mechanism, and most ship a review surface. Barbossa has ruled out worktrees by
premise. That makes the *other* half of the answer - knowing who touched what -
load-bearing rather than optional.

---

## 2. Evidence-backed gap analysis

### 2.1 The shared-checkout premise is undefended, not wrong

The premise is defensible, but only with evidence in hand. The market's own
critique is blunt: "a single shared checkout is a disaster when you spin up
multiple agents in parallel ... one agent changes branches, another edits the
same file, a third runs a cleanup command, and suddenly nobody knows which diff
belongs to which task"
([davidloor.com](https://davidloor.com/en/blog/how-to-keep-multiple-coding-agents-from-overwriting-each-other)).

The worktree camp is not actually winning on merit either. The same reporting
notes that worktrees "prevent parallel sessions from overwriting one another's
files on disk, but they do not prevent logical conflicts", producing "two
branches that merge cleanly at the file level but contradict each other at the
design level"
([Developers Digest](https://www.developersdigest.tech/blog/git-worktrees-claude-code-parallel-agents-guide)).
NTM, the most sophisticated of the tmux tools, ships worktrees *and* file
reservations, which concedes that isolation alone is not the mechanism.

Barbossa's README states the position honestly: "Nothing locks files: this is an
instruction-only contract, not enforcement." Anthropic's own agent-teams docs
give the same advice with the same absence of enforcement: "Two teammates
editing the same file leads to overwrites. Break the work so each teammate owns
a different set of files."

**Gap:** the contract is enforced only by the captain's attention. `create_crew`
in `agents.py:287` records `task` but nothing about which files the crew owns
or touches, so nothing in the system can detect that two live crew are writing
the same file. The captain is told to "give simultaneous writers disjoint files"
(`instructions.py:82`) and has no data with which to check whether it did.

### 2.2 No answer to "which crew changed what"

This is the auditability half of the stated audience's demand, and it is the
gap the market is actively filling. "One developer running four agents produces
four sets of changes under one name, from four different instructions, with
different amounts of review. Blame flattens all of that into a single identity"
([Mesa Agent Blame](https://www.mesa.dev/blog/agentblame-deep-dive)). A cluster
of tools exists purely for this: Agent Blame stores line attribution in git
notes, [agentdiff](https://github.com/codeprakhar25/agentdiff) hooks Claude Code
to record which agent wrote which line and signs it, ai-blame extracts
provenance from execution traces.

The pressure is not only aesthetic. Review has become the bottleneck: Faros AI
telemetry across 22,000 developers put median code review time up 441.5% while
throughput rose 33.7%
([CIO](https://www.cio.com/article/4207438/the-code-review-crisis-and-how-you-should-rebuild-review-models.html),
[FlowVerify](https://www.flowverify.co/blog/ai-code-review-bottleneck-2026-data)).
Trust is falling to meet it: 96% of developers do not fully trust AI-generated
code's functional accuracy, but only 48% verify before committing
([Tech Insider](https://tech-insider.org/ie/ai-code-quality-crisis-2026/)), and
the framing that has stuck is "the new bottleneck isn't writing code, it's
knowing what to trust"
([daily.dev](https://daily.dev/posts/the-new-bottleneck-in-ai-coding-isn-t-writing-code-it-s-knowing-what-to-trust--tf0cfa7o6)).

**Gap:** Barbossa's only record of what a crew did is the crew's own prose
report, stored as a graph edge (`crew.py:127`, `Crew.reports`). It is
self-reported, unstructured, and unverifiable. The hook event stream that would
carry ground truth is already wired up and currently subscribes to four
lifecycle events only (`instructions.py:157`).

### 2.3 Cost is controlled but invisible

Barbossa's `cheap`-by-default tier (`cli.py:66`) is a better cost posture than
any competitor surveyed, and worth saying so in the README. But control without
measurement leaves the user unable to tell whether it worked.

The numbers the market quotes: roughly $13 per developer per active day and
$150-250 per month, with agent teams specifically at "approximately 7x more
tokens than standard sessions"
([Claude Code cost docs](https://code.claude.com/docs/en/costs)). ccusage,
which reads the local session JSONL with no API key, has ~4,800 stars precisely
because this readout is missing everywhere else
([ccusage](https://ccusage.com/)).

**Gap:** `captain status` prints name, provider, model, pane, status, task
(`agents.py:202`). There is no token or dollar column, no session total, and no
per-crew budget cap, even though Claude Code exposes `--max-budget-usd` as a CLI
flag. A four-crew session's spend is unknowable from inside Barbossa.

### 2.4 Blocked crew are detected only while someone is waiting

`Crew.status()` correctly maps `PermissionRequest` and `Notification`
→ `blocked`, and `wait_crew` surfaces the tool name and input. That is good
plumbing. But it fires only inside an active `captain wait NAME` for one named
crew. A crew that blocks while the captain is waiting on a *different* crew
sits idle until someone asks about it.

This is the "babysitting" complaint the category keeps producing tools for:
notification unreliability "becomes more common with multiple concurrent
sessions"
([cmux #11975](https://github.com/manaflow-ai/cmux/issues/11975)), developers
asking how to "stay aware of what your AI coding agents are doing"
([Product Hunt discussion](https://www.producthunt.com/p/pushary/how-do-you-stay-aware-of-what-your-ai-coding-agents-are-doing)),
and dedicated status surfaces like
[CodeIsland](https://github.com/wxtsky/CodeIsland) existing for 13 different
agent CLIs. nudge's whole design point is delivering messages "only when idle".
Anthropic's agent-teams docs list the same friction under "Too many permission
prompts".

**Gap:** no way to wait on the *fleet*. The captain must poll `status` or guess
which crew to wait on.

### 2.5 Crew go straight to editing, with no plan gate

Anthropic ships plan mode for teammates specifically for "complex or risky
tasks", and its cost guidance recommends plan mode to prevent "expensive re-work
when the initial direction is wrong". Conductor's pitch is review-before-merge.
For an audience defined as wanting control rather than autonomy, going from a
one-line assignment straight to file edits is the wrong default for anything
non-mechanical.

**Gap:** `create_crew` submits the task immediately after the pane is ready
(`agents.py:419`). There is no flag to start a crew read-only.

### 2.6 Dismissal is irreversible with no safety check

`dismiss_crew` closes the pane permanently. The guard is a sentence in the
instructions telling the captain to "confirm with the user first if work is
unreported or uncommitted". Nothing checks. Given that only 48% of developers
verify AI code before committing, an instruction-only guard on a destructive,
unrecoverable action is the wrong weight.

### 2.7 Already solved - do not rebuild

Flagging these explicitly so they do not get re-proposed:

- **Hook-driven state, not pane scraping.** `crew.py` tails JSONL with cursor
  files; pane reading is fallback-only and is marked as such. Ahead of Claude
  Squad, dmux, nudge, and Tmux-Orchestrator, all of which infer state from the
  terminal.
- **Live fleet table.** `captain status` exists and refreshes from Herdr.
- **Cost *control*.** Cheap default tier plus in-place retier (`captain model`)
  is unmatched in the surveyed set.
- **Durable memory outside the repo,** with project/session scope split, safe
  pruning, and Graphify query. Agent teams explicitly cannot resume teammates;
  Barbossa's graph survives a captain restart.
- **Codex title-turn filtering,** draft-detection, and model-switch
  verification. These are the exact rough edges every pane-scraping competitor
  still has.
- **Follow-up without respawn** (`captain tell`), keeping pane and conversation.
- **Cross-session context loss** is largely handled by graph memory plus the
  `CAPTAIN_MEMORY` startup block. The transcript is gone by design; that is a
  documented trade, not a gap.
- **Herdr dependency / niche terminal.** A premise cost, not a fixable gap. Note
  it in positioning, do not engineer around it.

---

## 3. Ranked improvements

### P0-1. Record what each crew actually touches

- **Problem.** Nothing in the system knows which files a crew wrote. This blocks
  collision detection (2.1), auditability (2.2), and the dismissal guard (2.6)
  all at once. It is the single upstream fix.
- **Evidence.** Agent Blame / agentdiff exist purely for this; review time up
  441.5%; NTM ships file reservations alongside worktrees; Anthropic's own
  guidance ("break the work so each teammate owns a different set of files") has
  no enforcement behind it in any tool.
- **Smallest change.** Add `PostToolUse` to the hook event tuple in
  `instructions.native_args` (`instructions.py:157`) with a matcher limited to
  `Edit|Write|NotebookEdit`. The payload carries `tool_input.file_path`. The
  existing `append_event` already persists whatever arrives, and `event_status`
  returns `None` for unknown kinds so `wait` ignores them with no regression.
  Add a `Crew.touched` property that scans its own events file for those
  records. Roughly 20 lines across `instructions.py` and `crew.py`.
- **Cost.** One dict entry plus one property. Event files grow by one line per
  edit, which is small next to what already lands there. Codex has no per-tool
  hook, so Codex crew degrade to their self-reported file list; pi has no hooks
  at all. Document that asymmetry rather than papering over it.

### P0-2. Warn when two live crew touch the same file

- **Problem.** The shared-checkout premise is enforced only by the captain's
  attention. This is the objection every competitor's marketing leads with, and
  the one thing that would make the premise defensible instead of merely stated.
- **Evidence.** "Merge conflict factory"; "nobody knows which diff belongs to
  which task"; NTM's file reservations; Anthropic's overwrite warning.
- **Smallest change.** On top of P0-1: in `create_crew`, and as a marker column
  in `status_crew`, intersect the new crew's touched set against every other
  non-dismissed crew's. Print a warning naming the file and the other crew. Warn
  only, never block: the captain and user decide. Roughly 15 lines.
- **Cost.** Reads files already on disk, no new subsystem, no locking, no
  worktrees. Detection is after-the-fact rather than preventive, which is the
  honest ceiling of a no-lock design and should carry a `ponytail:` comment
  saying so.

### P0-3. Show what a session is spending

- **Problem.** Cost is controlled by tier default but invisible. The user cannot
  tell whether the cheap default is working, or what a four-crew session cost.
- **Evidence.** $13/dev/active-day and $150-250/month baselines; agent teams at
  ~7x a single session; ccusage at ~4.8k stars for exactly this readout; "long
  agentic sessions ... can move toward tens or even hundreds of dollars".
- **Smallest change.** Claude's hook payloads carry `transcript_path`. Persist it
  on the crew record the first time an event supplies it, then sum the
  `message.usage` token fields from that JSONL into a `TOKENS` column in
  `captain status`. Reuse the existing `read_events` reader. Do not compute
  dollars: token counts are ground truth, dollar figures need a pricing table
  that rots. Roughly 30 lines in `crew.py` and `agents.py`.
- **Cost.** Claude-only until Codex exposes an equivalent path; leave the column
  blank elsewhere. Follows the Graphify precedent of optional enrichment.
  Consider deferring dollars entirely to ccusage, which already handles both
  CLIs and is not our problem to re-solve.

### P1-4. `captain wait` with no name: wait for any crew

- **Problem.** A crew that blocks while the captain waits on another sits idle
  indefinitely. The captain must guess who to wait on.
- **Evidence.** Notification unreliability "more common with multiple concurrent
  sessions"; the babysitting complaint generally; nudge's idle-only delivery;
  CodeIsland across 13 CLIs; agent teams' "too many permission prompts".
- **Smallest change.** Make `wait.name` optional in the parser; when omitted,
  loop `read_events` across every non-dismissed crew's events file and return on
  the first non-`working` status. The per-crew reading logic in `Crew.status()`
  already exists and needs refactoring into a single-poll helper, not rewriting.
  Roughly 35 lines.
- **Cost.** The largest of the changes proposed, because `Crew.status()` mixes
  polling with its own timing loop and needs that loop lifted out. Still one
  module, no new file.

### P1-5. `--plan` flag for crew

- **Problem.** Crew go from a one-line assignment straight to edits. Wrong
  default for the control-oriented audience on anything non-mechanical.
- **Evidence.** Anthropic ships teammate plan mode for "complex or risky tasks"
  and recommends plan mode to prevent "expensive re-work"; Conductor's entire
  pitch is review-before-merge.
- **Smallest change.** `captain crew --plan` appends `--permission-mode plan` to
  the Claude flags in `native_args`. One flag, one list append, one README line.
  Roughly 8 lines.
- **Cost.** Claude-only; Codex has no verified equivalent, so the flag should
  error on Codex rather than silently doing nothing.

### P1-6. Guard `dismiss` against unreported or uncommitted work

- **Problem.** An irreversible, unrecoverable action guarded only by a sentence
  in a system prompt.
- **Evidence.** 96% do not fully trust AI output, 48% verify; 81% of enterprise
  tech leaders report more production issues from AI code.
- **Smallest change.** On top of P0-1: in `dismiss_crew`, run
  `git status --porcelain` limited to the crew's touched paths; if anything is
  dirty or the crew recorded no report, refuse and name the files, with the
  error text telling the captain to confirm with the user. Roughly 12 lines.
- **Cost.** One `git` subprocess on a destructive path. Must fail open if `git`
  is unavailable, matching how `project_root` already treats a missing git.

### P1-7. `--budget` passthrough

- **Problem.** No per-crew spend ceiling.
- **Evidence.** Cost blindness on long agentic sessions; Claude Code already
  exposes `--max-budget-usd` and compares it against its own session figure.
- **Smallest change.** `captain crew --budget N` appends `--max-budget-usd N`.
  Roughly 6 lines.
- **Cost.** Trivial. Claude-only. Verify the flag against the installed
  `claude --help` before shipping, the way the attribution setting was verified
  in `instructions.py:138`.

### P2-8. Make the crew report structured

Reports are free prose parsed by nobody. Asking crew to lead with a
`files: a.py, b.py` line would make them greppable and cross-checkable against
the P0-1 ledger, turning self-report into something verifiable. Cost: a few
words in the instruction block, which is token-budgeted, so only worth it once
P0-1 exists to check against.

### P2-9. Positioning section in the README

Barbossa's cheap-tier default, hook-driven state, and durable memory are ahead
of the field and nowhere stated as such. The audience is choosing between this
and Claude Squad or Conductor and has no basis for comparison. Cost: one README
table, no code. This is the cheapest thing on the list and probably the highest
ratio of value to effort for adoption specifically.

### P2-10. Promote file ownership to project scope

Once P0-1 lands, a durable `--scope project` edge recording which module a crew
name habitually owns would let a later session pre-assign disjoint work. Cost:
zero new code, an instruction line only. Genuinely speculative; do not build it
until someone asks.

---

## Sources

1. https://code.claude.com/docs/en/agent-teams
2. https://code.claude.com/docs/en/costs
3. https://github.com/smtg-ai/claude-squad
4. https://github.com/Dicklesworthstone/ntm
5. https://github.com/cottrell/nudge
6. https://github.com/standardagents/dmux
7. https://conductor.build/
8. https://github.com/Jedward23/Tmux-Orchestrator
9. https://www.augmentcode.com/tools/open-source-agent-orchestrators
10. https://abralo.com/alternatives
11. https://www.cio.com/article/4207438/the-code-review-crisis-and-how-you-should-rebuild-review-models.html
12. https://www.flowverify.co/blog/ai-code-review-bottleneck-2026-data
13. https://daily.dev/posts/the-new-bottleneck-in-ai-coding-isn-t-writing-code-it-s-knowing-what-to-trust--tf0cfa7o6
14. https://tech-insider.org/ie/ai-code-quality-crisis-2026/
15. https://ccusage.com/
16. https://www.mesa.dev/blog/agentblame-deep-dive
17. https://github.com/codeprakhar25/agentdiff
18. https://davidloor.com/en/blog/how-to-keep-multiple-coding-agents-from-overwriting-each-other
19. https://www.developersdigest.tech/blog/git-worktrees-claude-code-parallel-agents-guide
20. https://github.com/wxtsky/CodeIsland
21. https://github.com/manaflow-ai/cmux/issues/11975
22. https://www.producthunt.com/p/pushary/how-do-you-stay-aware-of-what-your-ai-coding-agents-are-doing
