# Crew mail 2: move delivery off the keystroke

Status: phases 1 and 2 implemented on `fix/comms-hook-delivery`, after 0.27.1.
Supersedes the transport half of [crew-mail.md](crew-mail.md) (shipped 0.22.0).
The mail directory, the receipt, the bounce and the assignment protocol in
[crew-lifecycle.md](crew-lifecycle.md) survive unchanged.

[What shipped](#what-shipped) records the delta, including two hook-contract
findings and one claim below that live testing disproved.

## The problem this solves

0.22 moved the *message* off the keystroke. The body became a file and the crew's
own `inbox` read became the receipt, which killed the `unknown` delivery and the
human `resolve` that cleared it. It left *delivery* on the keystroke: mail reaches
the model only because `ring` types a Herdr agent prompt into a composer, so every
gate on that typing is still a gate on the message arriving.

What that leaves today:

- the doorbell carries the payload on its first ring, so `ring` branches on
  whether an earlier ring landed and a landed retry must deliberately go
  content-free to avoid retyping work (`protocol.ring`, `pane.nudge`);
- `nudge_block` holds the ring behind gates read off the TUI, and the `user draft`
  gate holds forever: `draft_pending` returns `True` for any composer it cannot
  prove empty (`pane.py:137`), with no expiry anywhere. It wedged a live crew
  twice on 2026-09-30 and a human had to press Enter to clear it;
- `HELD_NOTICE_INTERVAL` (300s) exists to tell the captain that the wedge is
  happening, which is a timer around a bug rather than a fix;
- every crew prompt spends a line on *read your mail at the start of every turn*
  (`instructions.py:58`), so the receipt depends on the model choosing to act, and
  the `Stop` hook has to nag when it does not (`memory.stop_reason`);
- `deliver` pastes the identity block (crew name, assignment, incarnation,
  message, owned paths, allowed actions) into the body of every message
  (`protocol.py:369`), because the body is the only channel that reaches the model.

The cost is not the typing. It is that **the message is durable and the delivery
is not**: mail state is a file under a lock, and whether the crew ever sees it
still depends on what a TUI's composer looked like at one moment.

## The change

Delivery becomes a hook. The crew's own native hook, already running in the crew
process at prompt submission and at every turn boundary, pops its queue and hands
the body to its own model as context. The pane keeps exactly one job: starting a
turn in a crew that is idle.

```
captain assign/tell/answer ──▶ mail/<crew>/<id>.json
                                        │
                        (crew's own hook: pop ──▶ render ──▶ receipt)
                                        ▼
                                  crew's context
                                        ▲
    (doorbell: one fixed string, no payload, only to start a turn) ──┘
```

Four invariants. Each removes a class of bug rather than patching it:

1. **One channel of record.** Message text is never typed into a pane. The file
   is the message; the hook is the only thing that reads it to a model. A pane
   write can no longer be a delivery, so a composer quirk can no longer be a
   delivery failure.
2. **The receipt is the read.** The crew hook pops the queue and writes `read_at`
   in the same lock, in the crew process. No model action, no instruction line,
   nothing to drift past. `sent` loses its last reason to exist.
3. **The doorbell has no payload.** Every ring is the same fixed string. It is
   idempotent, safe to repeat, and safe to drop: dropping it delays a turn, it
   does not lose a message. There is nothing left to branch on in `ring`.
4. **No captain-side inference from pane text.** The captain stops reading a
   screen to decide whether a message may be sent. The one exception is liveness
   for hookless providers (`models.HOOKLESS`, today `pi` and `grok`), which have
   no event to read instead.

## Mechanism, graded per provider

No provider gets credit it has not earned. The hook contract differs per native
CLI, so the grade does too:

| Provider | Launch assignment | Mail to an idle crew | Mail arriving mid-turn | Receipt | Grade |
|---|---|---|---|---|---|
| `claude` | `SessionStart` `additionalContext`, or `initialUserMessage` | wake, then `UserPromptSubmit` `additionalContext`, same turn | `Stop` `additionalContext` at the boundary | hook-written | full |
| `codex` | typed prompt (unchanged) | `notify` wake, model reads | model reads at next turn | model-written | partial |
| `pi`, `grok` | typed prompt (unchanged) | typed wake, pane fallback | model reads at next turn | model-written | degraded |

**Claude is the full case**, and the published hook contract
([code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks)) carries
more than one way to hand a body to a model:

- `UserPromptSubmit` returns `hookSpecificOutput.additionalContext`, inserted
  alongside the submitted prompt and in context before the model responds. This is
  the primary delivery path for an idle crew: the content-free wake fires the hook,
  the hook pops the queue, and the mail is acted on in that same turn.
- `Stop` accepts `hookSpecificOutput.additionalContext` as well as
  `decision: block` with a `reason`. The docs separate the two: `reason` is shown
  to the user, while `additionalContext` is non-error feedback that continues the
  conversation. So the body rides `additionalContext` on both hooks and `reason`
  keeps its present job, a short nag about unfinished work. `Stop` is the boundary
  path for mail that arrives mid-turn, when no new prompt is coming.
- `SessionStart` returns `additionalContext`, and also `initialUserMessage`. The
  second is the candidate for deleting the typed launch prompt for a Claude crew
  outright: the assignment would start the first turn itself, with no pane write at
  all. Phase 1 proves it next to `additionalContext`.

The plumbing is nearly in place. Hooks are already registered for `SessionStart`,
`Stop`, `Notification` and `PermissionRequest` (`instructions.native_args`), so
`UserPromptSubmit` is one entry added to that tuple.
`protocol.queued(session_directory, crew_id)` already reads mail without a `Crew`
object, for exactly this kind of hook; the new work is a `pop` that renders and
stamps under one lock, and a `memory.stop_reason` that returns a nag while the
body goes out beside it.

Two consequences worth stating. `UserPromptSubmit` fires for a human's own prompt
into a crew pane as well as for our wake, so a person talking to their crew
delivers that crew's mail sooner, on a turn they started; this is a feature, but
the receipt is then written during their turn. And the wake string becomes
cosmetic: it is fixed, it carries nothing, and the hook supplies the real content
beside it.

**Codex is partial, and less than the brief for this doc assumed.** `notify` fires
at turn end, which is enough to know a boundary happened and enough to trigger a
wake, but it has no documented way to return context to the model: it is a program
invocation, not a hook with a stdout contract. So `notify` must not pop the queue
and must not write the receipt. A hook-written receipt there would mark mail read
that no model ever saw. Codex keeps the model-written receipt and the instruction
line, and gains only a reliable turn-boundary signal. Phase 4 verifies the stdout
claim first; if Codex ever grows a context channel, it moves to the Claude path
unchanged.

**pi and grok are degraded and stay that way.** `models.HOOKLESS` names them
because they have no hook at all: no delivery channel, no receipt channel, no
turn-boundary event. They keep the typed wake, the instruction line, the
model-written receipt and the pane fallback, with no pretence of parity. This is
also why the instruction line cannot be deleted globally in phase 3: it is deleted
for providers that have a delivery hook and kept for those that do not.
`instructions.agent_instructions` is already per-provider, so that costs nothing.

## Three rules we take from munder-difflin

Each is a rule they arrived at after a failure, and each maps onto a failure we
have in hand. Their line references are as reported by the captain; their tree is
not checked out here.

**Inferred blocks expire, and expiry fuses rather than erases.**
`terminalAutomation.ts:50,59`, `STALE_INPUT_MS` / `STALE_PICKER_MS` = `1_800_000`
(30 min). They reached for Ctrl-U first and destroyed real drafts, so the rule is
that a stale block times out and the pending write joins what is on the line
instead of clearing it. Our `user draft` gate has no expiry and no fuse, which is
how it wedged a live crew for the rest of a session. We take both halves: after 30
minutes the gate stops holding, and the wake is appended, never substituted for
the human's text. Fusing is tolerable here only because invariant 3 made the
doorbell content-free. Fusing a payload onto someone's half-written sentence would
be unacceptable; fusing a fixed wake string submits a 30-minute-old draft to its
own crew, which is a bounded accident and recoverable with an interrupt. Holding
forever is not recoverable without a human at that keyboard.

**The pane read is one-directional, with an echo grace.**
`terminalPool.ts:422`, `ECHO_GRACE_MS` = `1000`; `promptLineHasText` returns
`null` inside that window, and both call sites read
`inputDirty && promptLineHasText(...) !== false`. A screen read can therefore only
*clear* a draft block, never *invent* one. The asymmetry is the point: a wrong
"empty" fuses onto a human's sentence, while a wrong "has text" only parks a
message that is already durable. Allow only the cheap mistake. Ours is half right
already, since `draft_pending` treats an unreadable composer as a draft, but it is
missing the grace window: `nudge` types, and a drain that reads the composer a
moment later can see our own echo and report `user draft`, so a ring that landed
re-gates the next one. Inside the grace window the read returns unknown, and
unknown may not create a block.

**A held queue is visible, not silent.** Their "your draft" badge is derived at
render time from the same detection the gate uses, so the held state cannot
disagree with the thing holding it. That replaces `HELD_NOTICE_INTERVAL` and its
one-shot `noticed` flag in the `.drain` stamp. A held message becomes a derived
field on `captain status` and the dashboard, computed from the last ring's gate and
`held_since`, true whenever it is read and with no bookkeeping to get wrong.

## What this deletes

Five workarounds, each of which exists only because delivery was a keystroke:

- the *read your mail* line in every prompt, for providers with a delivery hook;
- the identity header `deliver` pastes into every body, which the hook renders
  from protocol state at delivery instead;
- `ring`'s landed / content-free branching, with `last_ring`'s `landed` field;
- `draft_pending` as a permanent wedge, replaced by expiry plus fuse;
- `HELD_NOTICE_INTERVAL`, `held_notice` and the `noticed` stamp, replaced by a
  derived field.

A sixth is a candidate rather than a plan: if `SessionStart` `initialUserMessage`
proves out, the typed launch prompt for a Claude crew goes too, and recruiting one
costs no pane write at all. That would also retire the first-doorbell-carries-the-
assignment fix from 0.26.1 for Claude, while Codex, pi and grok keep it.

`captain inbox` survives as a manual re-read. The message files stay on disk after
`read_at`, so a crew or a human can re-read what was delivered, which is also the
mitigation for a hook whose output is dropped.

## Where we beat them, and what our no-daemon rule costs

Their hook server only reports; the payload still arrives through the terminal, so
they need two wake paths, and `workerWake.ts` exists because Chromium throttles the
renderer timer that drives the first one. A Claude crew that picks its own mail up
in its own `UserPromptSubmit` or `Stop` hook needs no typing at all, so there is no
payload path to double up. The wake is only needed to start a turn in a crew that
is already idle, and it carries nothing when it does.

That is also the limit of the win. No daemon is a project rule, so a second wake
path is unavailable: the only things that ring a held doorbell are the captain's
`wait` loop and `pump_mail` before a captain-side command (`DRAIN_INTERVAL` 30s,
`DRAIN_LANDED_INTERVAL` 600s). An idle crew nobody is waiting on holds its mail
until some captain-side command runs. Mail is never lost and the receipt never
lies about that, but **delivery latency has no floor we control**, and we are
choosing that over a resident process.

## Costs we are not hiding

- **`tell` reaches a busy crew only at its next turn boundary.** A crew already
  mid-turn has no prompt submission left to ride, so its mail waits for `Stop`.
  Typing could land mid-turn, because Claude queues a prompt that arrives during a
  turn. This is a deliberate latency regression in exchange for invariant 1, and
  it is the only case that still pays it: an idle crew now acts on mail in the
  same turn the wake starts, because `UserPromptSubmit` injects the body beside
  the prompt before the model responds.
- **The receipt proves handoff, not comprehension.** The old receipt meant the
  model ran `inbox` and the text was in its context as tool output. The new one
  means the hook rendered the text and returned it. If a harness truncates or
  drops hook output, mail is marked read that no model saw. Two cheap mitigations:
  stamp `read_at` only after the hook's stdout is flushed, which makes delivery
  at-least-once and permits a duplicate rather than a silent loss; and keep
  `captain inbox` as a re-read of files that are still there.
- **Codex, pi and grok stay weaker**, as graded above. Mail to them is still
  delivered by a model action, so none of the hook-delivery guarantees apply there;
  invariants 1 and 2 hold for Claude and are aspirations for the rest.

## Phase 1 must prove the hook contract

The hook contract is documented
([code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks)), but
documented is not observed, and it is the load-bearing assumption of this whole
design. Nothing is deleted until a real session proves each of these:

- `UserPromptSubmit` `additionalContext` reaches the model verbatim and in full,
  at the length of a real assignment body, with no truncation;
- `Stop` `additionalContext` is honoured, and is honoured on a `Stop` that does
  *not* block. If context is only taken when the hook blocks, then mid-turn mail
  needs the block it no longer carries a body for, and the two outputs stop being
  independent;
- `SessionStart` `additionalContext` is in context before the first turn acts, and
  `initialUserMessage` starts that turn on its own. The second decides whether a
  Claude crew is recruited with no pane write at all;
- `stop_hook_active` behaves as observed. It is *not* in the public hooks
  reference, so our use of it (`memory.py:240`, never block twice in a chain) is an
  observed behaviour rather than a contract and may change under us. With the body
  on `additionalContext` the exposure is smaller than it looks: suppression costs
  the hold, not necessarily the delivery. Phase 1 measures which;
- a hook that raises still fails open. Today failing open means a crew may end its
  turn, which is benign. With delivery in the same hook, failing open means mail is
  not handed over on that boundary, so the queue must remain popped-only-on-success
  and the drain must still be able to ring again.

## Seam

```
protocol.queued(session_directory, crew_id) -> [message]   # unchanged, already hook-safe
protocol.pop(session_directory, crew_id) -> [message]      # pop + read_at in one lock
protocol.identity(assignment) -> str                       # header, rendered at delivery
protocol.held(crew) -> (gate, seconds) | None              # derived, for status/dashboard
memory.mail_context(events_path) -> str | None             # the body, for additionalContext
memory.stop_reason(events_path) -> str | None              # same signature, nag only
pane.wake(crew) -> None                                    # one fixed string, no payload
pane.nudge_block(crew) -> str | None                       # gates, now with expiry and grace
```

One renderer serves all three hooks: `mail_context` pops and renders, and the hook
wrapper puts the result in `hookSpecificOutput.additionalContext` for
`UserPromptSubmit`, `Stop` and `SessionStart` alike. `stop_reason` keeps its
signature and its present job, the short `reason` nag about unfinished work, and
stops being a pointer to mail.

`deliver` keeps its signature and loses its header, so `assign`, `tell` and
`answer` need no change at their call sites. `pane.nudge(crew, text=None)` becomes
`pane.wake(crew)` once no caller has a payload to pass.

## Phasing

1. **Hook delivery and a hook-written receipt for Claude.** `UserPromptSubmit`
   first, since it is the path an idle crew uses and the easiest to observe, then
   `Stop`, then `SessionStart` `additionalContext` and `initialUserMessage`. Both
   paths live and nothing is deleted: the hook delivers and stamps, the typed path
   and the instruction line stay as a fallback, and a duplicate delivery is
   expected and harmless. This is the phase that answers every question above.
2. **Content-free doorbell, draft expiry with fuse, one-directional read with echo
   grace, visible hold.** `ring` stops carrying a payload, the `user draft` gate
   stops being permanent, a screen read can only clear a block, and a held message
   shows up in `status` and the dashboard.
3. **Delete the five workarounds**, plus the typed launch prompt if
   `initialUserMessage` proved out. Only for providers that have a delivery hook,
   and only once phases 1 and 2 have run on a real session.
4. **Codex `notify` as a turn-boundary wake, pi and grok documented as degraded.**
   Verify whether `notify` can return context before trusting it with anything
   more than a wake.

Phase 3 is the only irreversible step, so it lands last and it lands narrow.

## What shipped

Phases 1 and 2, plus one thing this design did not cover: the captain's own idle
wake.

Delivery is a hook. `memory.deliver_mail` runs in the crew's own process on
`SessionStart`, `UserPromptSubmit` and `Stop`, and hands the mail to its own model
as `additionalContext`. `protocol.render` is the one rendering, shared by the hook,
`inbox` and the ring; `protocol.receipt` is the one writer of `read`, stamped only
after the body is flushed, so a hook that dies mid-write re-delivers. A Claude
crew's doorbell is `pane.WAKE_LINE`, payload-free. The stored body is the captain's
text alone.

Two corrections from the published hook reference. `Stop` carries
`additionalContext` independently of `decision`, so the body does not have to ride
the block reason and the worry in "Phase 1 must prove the hook contract" does not
arise. And `initialUserMessage` applies in `-p` mode only, so the sixth candidate
deletion, recruiting a Claude crew with no pane write, is not available and should
be struck rather than scheduled.

Mid-turn mail was the one thing live testing changed. This design says a `tell` to
a busy crew waits for `Stop`; in practice a crew calls `done` as its last action,
so `done` runs first and its unread-mail refusal pre-empts that boundary. The
refusal now carries the body itself. Both boundary vehicles hand the mail over: the
`done` refusal when the crew reports, `Stop` when it does not.

Delivering genuinely mid-turn was tried and reverted. Exempting hook-delivered crew
from `nudge_block`'s busy gate stopped one gate short, because a mid-turn pane has
no provable composer and `_draft_pending` held every ring as `user draft` anyway.
Lifting that gate too would type into a line that may hold a human's text. This is
parity rather than a shortfall: munder-difflin's drain requires `agent status is
idle` and firstmate's steering inbox waits on a busy pane as well.

What was missing is an interrupt, a different primitive from mail, now `captain
interrupt`.

`wait` no longer returns a bare timeout to a model. The default is a day, so quiet
stays quiet; `--timeout` still bounds a deliberate look. pi never had the problem,
because its `captain_wait` tool already rearmed silently.

Still open: `ring`'s landed branching and `last_ring`'s `landed` field, both still
load-bearing for the three providers without a hook; `HELD_NOTICE_INTERVAL` and its
`noticed` stamp, which want the derived `status` field described above; and phase
4, Codex `notify` as a turn-boundary wake.
