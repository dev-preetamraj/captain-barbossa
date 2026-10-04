# Quiet work

A Herdr pane plus a model is the most expensive thing a captain can spend, and it
spends it on work that needs neither. This plan sorts every task a captain can face
into three buckets, gives the one-step rule that picks a bucket, and says what to build
for the two that are not crew.

| Bucket | Needs | Costs | Mechanism |
| --- | --- | --- | --- |
| 1 | no model | one subprocess, zero tokens | `captain do`, `captain inspect` |
| 2 | a model, no pane | one turn, a 3-line prompt | `captain quiet` (new) |
| 3 | a model and a pane | pane + ~70-line instruction block + assignment + wait + report | `captain crew` |

## The decision rule

One step, three answers, asked before every task:

```text
No model needed?                                                  -> CAPTAIN do
A model, and you can write the whole instruction now,
  and one answer ends it?                                         -> CAPTAIN quiet
A model, and you will learn the next instruction
  from what it does?                                              -> recruit crew
```

The discriminator between 2 and 3 is **steering**, not risk, not size, and not whether
it writes files. If the captain already knows everything it would say to the agent, a
pane buys nothing: there is no next instruction to type into it. If the captain will
only know what to ask after seeing the agent's first move, nothing but a pane will do.

## Why captains recruit for bucket 1 today

The mechanism is complete: `do.py` has `commit`, `push`, `branch` and `run <target>`,
`inspect git status|diff|log` finds the paths, `cli.py` dispatches `do`, and
`instructions.py:88-96` tells the captain all of it. Nothing is missing. The captain
instructions argue against themselves, and the lines that lose are the general ones.

1. **The tier table licenses exactly what `do` forbids.** The `do` block says a commit
   or a declared target "is not crew work" (`instructions.py:88-96`). Twenty lines
   later, inside the ruleset marked "for EVERY creation", the model tier table reads
   "cheap for mechanical work (commits, tests, lint, formatting, ...)"
   (`instructions.py:107-108`). That is a concrete, named list of crew work, sitting in
   the block the captain reads at recruit time, and it names commits and tests first.
   A general classification rule loses to a specific worked example every time.

2. **The self-check asks the recruiting question first.** "does this need judgment?
   Recruit crew. Is the answer fixed by the inputs? CAPTAIN do."
   (`instructions.py:99-101`). The first branch is the wide one and it answers yes for
   almost anything with a human request behind it: a commit needs a *message*, which is
   authored text, so "commit and push" classifies as judgment before the second
   question is ever reached. Order is most of the fix.

3. **No line maps the user's phrasing to a bucket.** The `do` block is written in
   categories ("outcome its inputs already determine"), never in the words a user
   types. "Commit and push what Gibbs did" arrives as a sentence about files and people.

Ruled out as causes: `guard_crew` blocks `do` for crew only, as intended; the captain's
`{command}` carries `--session`, so `read_session` resolves; `RUNNABLE` contains `gate`
and `test`, which this project's Makefile declares. One real but separate gap:
`captain do` appears in `AGENTS.md` and not in the README command table, so users cannot
discover it either.

A crew that already exists still commits its own hunks. The waste is recruiting a pane,
a model and a report whose entire assignment is one command.

## Bucket 1: no model at all

Two commands already split this cleanly, and the split is the rule for new verbs:
**reads go to `inspect`, writes and anything needing the real environment go to `do`.**
Anything read-only that `inspect` already answers must not become a `do` verb.

The safety property to preserve: **a verb is a word, never a flag.** The whole argument
list is a literal inside the module, so no option reaches the tool that the module did
not write. `--force`, `--hard`, `-f` and `--amend` are not refused at runtime, they are
unreachable. Where a caller supplies data (a branch name, a grep pattern, a path) it is
validated and passed after a terminator - `-e` for a pattern, `--` for paths - so data
cannot become an option.

The third mechanism matters as much as the two commands: **`do run <target>` is the
general escape hatch**, limited to names in `RUNNABLE` that the project itself declares.
Most "the captain shelled out for this" cases are a missing Makefile target, not a
missing feature in this tool.

### Reads - `inspect`

| Capability | In/out | Why |
| --- | --- | --- |
| file read, directory listing, literal search | in, exists | `inspect read/files/search` |
| `git status`, `diff`, `diff --staged`, `log`, `current-branch`, `root` | in, exists | `inspect git <operation>` |
| branch list | **in, added** | a captain could create a branch and not see which exist |
| tracked file list | **in, added** | `inspect files` walks the real worktree, so `.venv`, caches and build output eat the 200-path limit before the project's own files appear. `git ls-files --cached` is the honest answer to "what is in this project" |
| tracked-file search | **in, added** | same gap for `inspect search`, which also walks `.venv` and burns its 8 MiB scan budget there. `git grep --fixed-strings -e TEXT --` is .gitignore-aware and fast |
| the event log, mail, assignment records, `captain.json` | in, exists | `inspect state session <path>`. No new code: the session directory is already a readable scope, and `protocol.json` and `mail/` live inside it |
| token usage and cost | **in, added** | `dashboard.render` was already a pure function with only a refresh loop wrapped round it, so `captain dashboard --once` prints one frame with no pane. The only previous way to read cost was to watch a pane |
| crew pane contents | in, exists | `herdr agent read <name>`, already in the captain's instructions |
| declared target list | in, exists | `make help` via `do run help`, or `inspect read Makefile`. A `do targets` verb would be a third spelling of the same answer |
| the project version | in, exists | `inspect read pyproject.toml` |
| `git show`, `blame`, `shortlog` | out | no caller in a captain's loop: `log` gives subjects and `diff` gives content. Crew read history, captains route work |
| `git diff REV..REV` (a branch against main) | out, listed below | the one genuinely missing read, but it needs caller-supplied revisions and a default-branch guess |
| tag list, `remote -v`, `stash list` | out | no caller. Releases are the user's, a missing remote already fails `do push` loudly, and a stash the captain cares about means uncommitted work in play, which is the user's |

### Writes and environment - `do`

| Capability | In/out | Why |
| --- | --- | --- |
| `commit --message [PATH...]`, `push`, `branch NAME`, `run <target>` | in, exists | |
| `switch NAME` | in, added | the counterpart to `branch`; Git refuses on its own when the switch would overwrite local changes |
| `fetch` | in, added | touches no worktree file at all |
| `pull` (`--ff-only` hardwired) | in, added | fully determined: it moves the ref or exits non-zero, so no conflicted tree can appear and the merging form is not a flag anyone can pass |
| every failed action is recorded | **in, fixed** | the module promised "every run is recorded" and only recorded successes: `_spawn` raised before `record` ran. A failed `make gate` is the run most worth reviewing and it had no pane to review. Now `_act` records, then raises |
| `help` in `RUNNABLE` | **in, added** | a declared `help` target only prints, in this project and by convention everywhere. It is the cheapest answer to "what can this project do" |
| dependency sync, lockfile check, tool versions | in, exists | `do run install` and `do run check`/`gate`, when the project declares them. This project's `install` is `uv sync --locked`. Wiring `uv` directly would hardcode one ecosystem where a declared target already generalises |
| one test or one test file | out, listed below | the gap most felt in practice, and language-specific |
| lint or format scoped to named paths | out, listed below | same |
| `version` in `RUNNABLE` | out | here it only prints, but the name is a bump as often as a read, and the whitelist is name-based with no view of the recipe |
| `status` in `RUNNABLE` | out | here it curls PyPI; the name promises nothing in particular |
| `tool` in `RUNNABLE` | out | `uv tool install --force .` mutates the user's global tool environment |
| `fmt`/`format` in `RUNNABLE` | **in today, questioned below** | `make format` rewrites the whole tree, including other crew's uncommitted work, against the editing contract |
| non-Make task runners (`package.json` scripts, `Justfile`, `tox`, `cargo`) | out | YAGNI: this project has only a Makefile, so a runner-detection layer would be written blind and tested against nothing. One `_targets` parser for one declared file is the whole need today |
| `merge`, `rebase`, `amend`, `reset`, `restore`, `clean`, `rm`, force push | out | rewrite or discard |
| `stash`, `stash pop` | out | hides uncommitted work |
| `tag`, release, publish, deploy, version bump | out | outward-facing compound acts |
| bare `add` | out | `commit` already stages |
| `gh pr create`, `gh pr view`, `gh run list` | out | publishes outward, or reads an external service over the network with no auth story; its body is authored text, which bucket 2 drafts and the user posts |
| arbitrary shell | out | the declared-target set is the safety property |

## Bucket 2: a model, no pane

A pane buys two things: somewhere to approve a native permission prompt, and somewhere
to steer a running turn. Work needing neither should pay for neither. Modelled on
`~/code/firstmate/bin/fm-supervision-engine-lib.sh:347-360`: one headless invocation,
`-p` with the task, `--output-format json`, a structured result, no window.

The enumeration below is the captain's own recurring model moments, from this session and
from the code - not a generic list. What decides between "plain `quiet --task`" and "a
named verb" is one question: **can the turn get the input by itself?** It has `Read`,
`Grep`, `Glob` and this session's directory, so it can read files, the event log, mail
and assignment records. It has no shell, so it cannot read Git. A named verb earns itself
only where it hands over something the turn cannot fetch.

| The captain's moment | Shape | Why |
| --- | --- | --- |
| draft a commit message from the staged diff | **`--diff staged`** | Git is the one thing the turn cannot read |
| review a crew's diff before dismissing them | **`--diff worktree`** | same |
| check a diff against AGENTS.md | **`--diff`** | the turn reads AGENTS.md itself; only the diff has to be handed over |
| draft a PR body or a changelog line | **`--diff`** | same |
| triage a failing suite into real breakage and pinned-wording assertions | plain `--task` | the output is already in the captain's context, or in `events/captain.jsonl`, which the turn can now read |
| verify a crew report against the raw output file it claims | plain `--task` | naming the file is enough; the turn has `Read` |
| explain a traceback | plain `--task` | the traceback is in the captain's context |
| draft a crew assignment prompt from a user sentence | plain `--task` | pure text in, text out |
| name a branch from a description | plain `--task` | a one-word answer on the cheap tier |
| answer a question about the code | plain `--task` | `Grep` and `Read` are the whole job |
| a second opinion on a plan | plain `--task` | the turn reads the plan file |
| one bounded edit | `--write PATH...` | a rename in named files, a docstring, a doc line |

So bucket 2 needs exactly **one** new flag, `--diff worktree|staged`, which collapses
four of the twelve moments. Everything else is the same command with different words in
the task, and a verb per phrasing would be a thesaurus, not a feature.

Two supporting decisions fall out of the same question:

- **A read-only turn is given `--add-dir <session directory>`**, so it can read the event
  log, mail and assignment records it is asked to triage or check a claim against.
  Without it the turn's working directory is the project alone and that state is
  unreachable, which would force the captain to paste.
- **A turn with `--write` is not given it**, so an unwatched editing turn can never touch
  mail, assignment records or the audit log. One condition, and the widest exposure is
  confined to the narrowest turn.

### Always offload, never "I could just read it"

A live test found the mirror of the defect this document opens with. A fresh captain
asked "what does placement.py do?" read `placement.py`, `layout.py` and `prompts.py`
itself: 17 seconds of multi-file reading, all of it permanent in its context, and its
`events/captain.jsonl` held five `do` records and zero `quiet` records. It never
considered `quiet` at all.

The cause was the same shape as the original: a line that blessed the expensive path.
"Direct read/search, bounded CAPTAIN inspect, memory reads, answers, and coordination
commands are allowed" reads as a licence to answer any question directly, and the
do/quiet/crew rule is only consulted when the captain is deciding whether to *delegate*.
Answering a question does not feel like delegating, so the rule was never reached.

The fix is not a better classification test, it is removing the choice: **a question that
needs a file the captain has not read goes to `quiet`, even when the captain could answer
it by reading.** The captain's context is the scarce resource. A quiet turn spends a
throwaway one and returns a paragraph; the captain spends its own and keeps three files
forever. The one-step rule is untouched - this sits above it, because the question never
had to reach it.

### Design

**Lives in `do.py`**, not a new module. Same premise as that file's docstring (work the
captain does itself, recorded), and it reuses `do.record` for the audit line, whose
record type is already literally `"quiet"`.

It cannot reuse `do._spawn`, which the build proved: `_spawn` returns
`(stdout + stderr).strip()`, and anything the CLI writes to stderr would make the JSON
result unparseable. The subprocess body is now `do._capture`, returning the
`CompletedProcess`; `_spawn` is a four-line wrapper over it that combines and checks, so
no existing call site changed.

### Every provider, each with its own gate

A quiet turn that only claude can take is the original defect wearing a different hat: a
codex, pi or grok captain would get a refusal and recruit a pane. So the per-provider
knowledge lives in `models.py` as two functions - `headless_argv` builds one provider's
whole literal argv, `headless_report` reads one provider's own result document - and
`do.py` keeps the session, the record and the diff and knows none of it.

Forcing one shape on four CLIs would mean using the weakest gate each has. Each gets its
strongest instead. Every row below was run against the installed CLI, not read from a
help page; the write gate was tested by telling the turn to create a file and then
looking for the file.

| | claude 2.1.289 | codex-cli 0.158.0 | grok 1.0.46 | pi 0.87.1 |
| --- | --- | --- | --- | --- |
| headless mode | `-p TASK --output-format json` | `exec --json TASK` | `-p TASK --output-format json` | `-p --mode json TASK` |
| other flags | `--permission-mode dontAsk` | `--ignore-user-config --skip-git-repo-check --ephemeral` | `--trust --permission-mode dontAsk` | `--no-session --no-context-files` |
| read-only gate | `--allowedTools Read Grep Glob` plus `--disallowedTools Bash Write Edit` | `--sandbox read-only` | `--deny Write --deny Edit --deny Bash` | `--tools read` |
| write gate | adds `Edit Write` to the allowlist, drops the denylist | `--sandbox workspace-write` | drops the Write/Edit denies, keeps `--deny Bash` | `--tools read,write,edit` |
| reads the session directory | needs `--add-dir`; its reads are path-checked | nothing needed: the read-only sandbox reads the whole filesystem | nothing needed | nothing needed |
| result reader | one JSON object: `type=result`, `subtype=success`, `is_error=false`, text in `result` | JSONL: the **last** `agent_message` item, and a `turn.completed` event must exist | one JSON object: `stopReason=end_turn`, text in `text` | JSONL: the last `turn_end`, `stopReason` in `("stop", "end_turn")`, text joined from `message.content` |
| spend reported | `num_turns`, `total_cost_usd` | output tokens only | `num_turns`, `total_cost_usd` | `usage.cost.total`, `usage.totalTokens` |
| write blocked, live | yes | yes, **only** with `--ignore-user-config` | yes | yes |
| success read, live | yes | yes | yes | yes, on the fourth attempt - see below |

Four findings came out of running it rather than reading about it:

1. **`codex exec --sandbox read-only` does not hold on its own.** The first live run
   created the file anyway. The cause is in the user's `~/.codex/config.toml`:
   `approvals_reviewer = "auto_review"` and a list of trusted projects. Adding
   `--ignore-user-config` makes the sandbox hold, and auth still comes from `CODEX_HOME`.
   A gate a user's own config can silently turn off is not a gate, so that flag is part of
   the argv, not an option.
2. **The exit status is not a completeness test.** pi exited 0 with
   `stopReason: "error"`, empty content and a `provider_transport_failure` diagnostic -
   four times, for three different providers. So every reader decides from the document
   and an unrecognised shape is a failure, never an answer.
3. **Codex narrates before it answers.** Its stream carried `"I'll create the file with
   the requested contents."` as one `agent_message` and `"DONE"` as the next. The reader
   takes the last one; taking the first would have reported the opposite of what happened.
4. **pi does not say `end_turn`.** A successful pi turn ends with `stopReason: "stop"` -
   pi relays its provider's own word, and the live turn ran on xai. The reader was written
   against grok's `end_turn`, so it would have raised "did not complete" on *every*
   successful pi turn: quiet would have been broken for pi in exactly the way this round
   set out to fix. It accepts both words now and still rejects `error`, `toolUse` and a
   truncation. This only surfaced because a probe left running in the background finished
   after the first report had gone out.

**pi took four attempts to verify, and the fourth is why it works.** openai-codex failed
with a WebSocket error twice and ollama was not running; the xai run outlived a
five-minute foreground wait and only returned later, in the background. That late result
carried the `stopReason: "stop"` correction above. pi is now verified the same way as the
others: the shipped argv read a file outside the cwd, `--tools read` blocked the write
with no file created, and the reader returned the report and a spend note. One pi event
stream is worth more than any amount of reasoning about its shape - the reader had been
wrong in a way no amount of re-reading pi's `--help` would have shown.

**No provider is left out.** All four have a headless mode and a read-only gate, so the
refusal only fires for a provider that does not exist yet:

```text
<name> has no headless turn to spend, so there is no way to bound what an unwatched one
could do. Recruit crew instead.
```

### Design

The quiet prompt leads the task rather than riding a system-prompt flag. claude, pi and
grok all accept `--append-system-prompt` (grok as an undocumented compat alias) and codex
wants `-c developer_instructions=...`; for a single-turn agent three sentences at the top
of the task behave identically and need no fourth code path.

Flow, as a `quiet` action in `do.run`:

1. `provider = read_json(session_directory / "captain.json")["provider"]`, already
   written by `agents.launch`; no new state.
2. Not in `models.PROVIDERS` -> the refusal above.
3. `model = resolve_model(provider, args.model or config.text("crew", "model"))` - the
   same tier resolution and the same `cheap` default a crew gets.
4. Each `--write` path through the existing `_inside(root, value)`, so `.git` and anything
   outside the project are refused before the model starts. The named paths are also
   appended to the task text, because every provider's write gate is a tool or sandbox
   gate, not a path gate.
5. With `--diff`, append `inspection.git_text(root, "diff", staged=...)` to the task,
   truncated to `DIFF_LIMIT` (6000, under `check_text`'s 8000 cap on the task itself).
   This reuses the hardened Git read rather than adding a second one.
6. `_capture(root, headless_argv(provider, executable(provider), task, writable=...,
   add_dir=..., model=...))`. `_capture` passes `stdin=subprocess.DEVNULL`, because codex
   reads stdin when it is not a TTY and appends it to the prompt as a `<stdin>` block.
7. `headless_report(provider, done.stdout)` - the report and a spend note, or a raise.
8. `record(session_directory, "quiet", command, code, report)`, print the report, print
   the spend note to stderr.

**Lives in `do.py`**, not a new module, with the provider table in `models.py`. Same
premise as `do.py`'s docstring (work the captain does itself, recorded), reusing
`do.record` for the audit line, whose record type is already literally `"quiet"`.

It cannot reuse `do._spawn`, which the build proved: `_spawn` returns
`(stdout + stderr).strip()`, and anything the CLI writes to stderr would make the result
unparseable - codex writes a model-refresh error there on every run. The subprocess body
is `do._capture`, returning the `CompletedProcess`; `_spawn` is a four-line wrapper over
it that combines and checks, so no existing call site changed.

### Why an unwatched edit is allowed, and where it stops

`--write` takes explicit paths and nothing else, and a turn with no `--write` gets no
editing tool at all. With `--write`, be exact about what the bound is: every provider's
write gate is a tool gate or a sandbox, not a path gate, so the named files are a
validated instruction rather than an enforced boundary. Codex is the only one with any
boundary at all - `--sandbox workspace-write` confines it to the workspace - and claude,
grok and pi could in principle edit any file in the checkout. The enforced part is what comes after - the turn leaves its edits
**unstaged in the worktree** and its report in `events/captain.jsonl`, so the captain
reviews it with `inspect git diff` exactly as it reviews a `do commit`. That is the same
bargain bucket 1 already makes: unwatched is acceptable because it is reviewable. A
per-path tool pattern would tighten it, but the syntax is CLI-version-specific and
unverified here, so it is not claimed. The ceiling is real and worth naming:

```python
# ponytail: one-shot and uninterruptible, so a quiet edit is bounded by its named paths
# and reviewed from the diff. Iterative or unbounded editing needs a pane, not a wider
# --allowedTools.
```

**CLI**: `do.add_arguments` registers a second top-level parser beside `do`, keeping the
surface in one module.

```text
captain quiet --task 'instruction' [--diff worktree|staged] [--write PATH...]
              [--model cheap|mid|strong|<model>] [--timeout SECONDS]
```

`cli.main` routes it to the same `do.run` with `args.command in ("do", "quiet")`, and
the parser carries `set_defaults(do_command="quiet")`, so `do.run` keeps its single read
of `do_command` and needs no knowledge of `args.command`. Crew already cannot run it:
`guard_crew` refuses every command it does not name, so `quiet` needs no entry there,
only a test pinning the refusal.

## Bucket 3: a model and a pane

What stays crew, and the reason is the same every time - the captain cannot write the
whole instruction up front:

- **Debugging.** Hypothesis, test, next hypothesis. The second instruction does not
  exist until the first result does.
- **A feature or change across files.** The plan changes as the files are read.
- **Design, architecture, planning.** May need the user mid-flight, and only crew have
  `ask`.
- **Anything that will hit a native permission prompt.** Approval needs a pane to read
  and a key to send; a headless turn can only be denied.
- **Anything the user may want to watch, interrupt, retier, or hand off.** `interrupt`,
  `model`, `tell` and `--handoff` all address a living pane.
- **Long-context work**, where the useful context is built across turns.
- **Work that outlives one timeout.**
- **Work that commits its own hunks**, per the convention that crew own their commits.

Investigation sits on the line and splits by the rule: one question is `quiet`, an
open-ended "find out why this is slow" is crew.

## Instruction deltas (`src/captain_barbossa/instructions.py`)

Token cost is characters over four. Net as built: **+10 lines, ~+161 tokens** for the
first six deltas, then **+3 lines, ~+60 tokens** for the always-offload rule (G) - **+13
lines, ~+221 tokens** in all, per captain session. None of it reaches crew sessions: this
block is captain-only. The crew block's own budget (`crew_words <= 350`) is untouched and
sits at 345, five words of headroom.

**H. Stop naming quiet's own work as crew work.** The tier table's last pull, 2 lines ->
2 lines, **+9 tokens**. Delta A took out the four names `do` absorbs; these four are what a
`quiet --write` turn absorbs, so the cheap tier now names work that is simple *and* needs
watching, which is the only thing a cheap crew is for.

```text
- cheap. Pick the tier by how complex the assignment is: cheap for mechanical edits
- (docs, chores, renames, small mechanical changes), mid for a
+ cheap. Pick the tier by how complex the assignment is: cheap for simple work that still
+ needs watching - a focused fix, or following a pattern the codebase already has - mid for a
```

**A. Stop naming commits as crew work.** `instructions.py:107-108`, 2 lines -> 2 lines,
**-6 tokens**.

```text
- cheap. Pick the tier by how complex the assignment is: cheap for mechanical work
- (commits, tests, lint, formatting, docs, chores, renames, mechanical edits), mid for a
+ cheap. Pick the tier by how complex the assignment is: cheap for mechanical edits
+ (docs, chores, renames, small mechanical changes), mid for a
```

**B. Name the phrasing.** Insert after `instructions.py:95`, +2 lines, **+37 tokens**.

```text
+ "Commit and push", "cut a branch", "run the gate", "what does this do" never justify
+ a recruit, whatever else is running and whoever wrote the files.
```

**C. Replace the self-check with the one-step rule.** `instructions.py:99-101`, 3 lines
-> 6 lines, **+33 tokens**. The reorder alone is most of the fix: the cheapest branch is
asked first instead of last. The words "Self-check first" are kept so the existing
crew-side `assertNotIn` keeps its teeth.

```text
- commands are allowed. Self-check first: does this need judgment? Recruit crew. Is the
- answer fixed by the inputs? CAPTAIN do. Work directly outside both only if the user
- says "yourself", "no crew", or "do not recruit".
+ commands are allowed. Self-check first, in this order:
+ No model needed? CAPTAIN do.
+ A model, and you can write the whole instruction now and one answer ends it? CAPTAIN
+ quiet.
+ A model, and you will learn the next instruction from what it does? Recruit crew.
+ Work outside all three only if the user says "yourself", "no crew", or "do not recruit".
```

**D. Introduce `quiet`.** Insert after the `CAPTAIN do run ...` line, +4 lines,
**+81 tokens**.

```text
+ One model turn with nothing to steer - a question about the code, a short review, a
+ summary, triage, a drafted commit message, one bounded edit - needs no pane either. One
+ headless turn, result printed and recorded, no assignment and no wait:
+   CAPTAIN quiet --task 'instruction' [--write PATH...] [--model cheap|mid|strong]
```

**E. The three new `do` verbs.** Insert after `instructions.py:95`, +1 line,
**+15 tokens**.

```text
+   CAPTAIN do switch NAME | fetch | pull (fast-forward only)
```

`inspect git branches` needs no instruction line: the `inspect` operations are already
discoverable from `--help` and the captain reaches them without being told each one.

**G. Always offload a question that needs an unread file.** Replaces the line that
blessed answering directly, 2 lines -> 5 lines, **+60 tokens**. The two licences removed
are "Direct read/search" and the bare word "answers"; `answer` itself survives under "the
coordination commands below", where it was already listed.

```text
- Direct read/search, bounded CAPTAIN inspect, memory reads, answers, and coordination
- commands are allowed. Self-check first, in this order:
+ Answer from what you have already read. Any question that means opening files you have
+ not read goes to CAPTAIN quiet, whatever you could answer by reading them yourself: the
+ captain's context is the scarce resource, and a quiet turn spends a throwaway one.
+ Bounded CAPTAIN inspect, memory reads, and the coordination commands below are allowed.
+ Self-check first, in this order:
```

Line 155-157 (`never add a commit step to a crew assignment`) stays as written. It
encodes the convention that crew commit their own hunks and the captain only picks up
the user's leftovers; it is about *who commits*, not about *whether to recruit*.

## Checks

`tests/test_do.py`, extended, no new file. The git verbs run against a real bare remote
in a temp directory, so `fetch` and `pull` are exercised rather than string-matched.

1. **The safety property, over every verb.** Run `fetch`, `pull`, `commit`, `push`,
   `branch`, `switch`, `run`, then assert no recorded command holds `--force`, `-f`,
   `--hard`, `--amend`, `reset`, `rebase`, `--no-ff` or `--rebase`. This generalises the
   existing `RUNNABLE` assertion from one verb set to all of them. Writing it proved the
   property twice over: with the verbs in any other order the test fails, because `push`
   refuses a remote that is ahead and `pull` refuses a diverged history instead of
   reaching for a flag that would have resolved either.
2. **`pull` fast-forwards, and refuses a history it would have to merge** - two cases
   against the real remote, the second asserting the remote's file never arrives.
3. **`quiet`, success**: a fake `claude` on `PATH` printing a success result. Asserts the
   task reaches `-p`, the printed output is `result` and not the raw JSON, and
   `events/captain.jsonl` gained a `"type": "quiet"` line.
4. **`quiet` is read-only until `--write` names a file**: `Read`/`Grep`/`Glob` always,
   `Edit`/`Write` only with `--write`.
5. **`quiet --write` refuses a path outside the project or inside `.git`**, reusing the
   `_inside` cases already written for `commit`, and records nothing.
6. **`quiet` refuses an incomplete turn**: `is_error: true`, and separately a stdout that
   is not a result document.
7. **`quiet` needs a captain with a headless turn**: provider `codex` and a missing
   `captain.json` both raise.
8. **Crew may not run `quiet`**: `cli.guard_crew` refuses it under `CAPTAIN_ROLE=crew`.
9. **A failing target is recorded before it is raised**, with `make`'s own exit status.
10. **`help` runs** and its output reaches the record.
11. **`--diff` hands over the worktree and staged diffs**, and a long one is truncated
    rather than sent whole.
12. **Only a read-only turn gets `--add-dir`**; a `--write` turn gets neither the flag nor
    the path.

`tests/test_inspection.py` covers the two new reads: tracked reads skip what the worktree
walk steps over (an ignored file appears in `inspect files` and in neither `ls-files` nor
`grep`), a no-match `grep` is an empty answer rather than a failure, and a pattern that
looks like an option stays a pattern while an empty, oversized or misplaced `--text` is
refused.

`tests/test_instruction_size.py` pins delta G in the same shape as the rules beside it:
the sentence is present, "Direct read/search" and "memory reads, answers, and
coordination" are gone, the trimmed allowance is present, and the crew block never sees
"the scarce resource".

`captain dashboard --once` needs no new test: it prints `dashboard.render`, a pure
function `tests/test_dashboard.py` already covers, from the same session record
`run_dashboard` builds on its first line.

`switch` also joins the existing branch-name refusal test, since both verbs share
`do._name`.

## Needs the user's call, not built

1. **One test or one test file**, and **lint or format scoped to named paths.** These are
   the two gaps actually hit while building this: both were shelled out for. Neither fits
   `do run <target>`, because the target name cannot carry an argument without reopening
   flag injection, and both are language- and tool-specific. Shipping them means deciding
   that `do` becomes tool-aware - either a `[do]` settings key per project
   (`test_one = "uv run --locked python -m unittest {name}"`, with a validated `{name}`)
   or a detected command table per project type. That is a design decision about what
   this tool knows, so it is the user's.
2. **`fmt` and `format` in `RUNNABLE`.** `make format` here runs `ruff check --fix .` and
   `ruff format .` over the whole tree. In a shared checkout that rewrites files the
   captain does not own, including other crew's uncommitted work, which the editing
   contract forbids in the same breath as `git add -A`. By the rule that anything which
   rewrites stays out, both names should leave `RUNNABLE`. Not done here because it
   removes a shipped verb.
3. **`version` and `status` as `RUNNABLE` names.** In this project `make version` only
   prints and would be useful; the whitelist is name-based and cannot see the recipe, so
   admitting `version` admits a bump elsewhere. If the user wants per-project exceptions,
   that is a settings decision, not a name decision.
4. **`git diff REV..REV`,** to review a feature branch against main before merging. The
   one read I would add next. It needs caller-supplied revisions (safe behind the same
   validation `do branch` uses, plus `--`) and a way to know the default branch's name,
   which is a guess (`main` or `master`) unless the user names it in settings.
5. **The session directory being readable by a quiet turn.** Implemented, because
   otherwise triage and report-checking cannot work without pasting. It does mean a
   read-only turn can read this session's mail and other crew's assignment records. If
   that exposure is unwanted, dropping the `--add-dir` line is a one-line change.

## Skipped

- **A system-prompt flag per provider for `quiet`.** Three sentences at the top of the
  task do the same job for a single-turn agent and cost one code path instead of four.
- **`gh` anything.** No network reads, no PR creation; needs an auth story first.
- **Approvals, interruption, mail, `ask`/`done`, name reservation, a protocol assignment
  for `quiet`.** It is a function call, not a crew member.
- **Multi-turn and `--resume`.** One shot. A second question is a second `quiet`.
- **The crew instruction block as the quiet prompt.** ~70 lines describing commands a
  headless session cannot run; 3 lines of `QUIET` instead.
- **Dashboard and `usage.py` integration.** The JSON already carries `total_cost_usd`;
  printing it beats a crew row for a process that has already exited.
- **Parallel or queued quiet runs.** Foreground, one at a time, bounded by `do.TIMEOUT`.
- **A `quiet` verb per phrasing** (`quiet commit-message`, `quiet triage`, `quiet review`).
  Ten moments, one flag: the rest differ only in the words of the task.
- **A `do targets` verb and a `do version` verb.** `make help` and `inspect read` already
  answer both.
- **A config switch for any of this.** No setting for a value that never changes.
- **Auto-routing.** The captain picks the bucket from the rule; nothing inspects a task
  string to choose for it.
