# Crew mail: move the message off the keystroke

Status: shipped in 0.22.0. Supersedes nothing; it changes how `assign`, `tell`
and `answer` reach a crew, and leaves the assignment protocol in
[crew-lifecycle.md](crew-lifecycle.md) intact.

## The problem this solves

Before 0.22.0 a message to a crew *was* a terminal write. `deliver` persisted the
message, then typed it into the native CLI's composer and inferred from the pane
whether it landed. Every quirk of that composer was therefore a delivery failure:

- a contextual suggestion (`Try "refactor agents.py"`) read as a user draft, so
  the send was refused;
- a native permission prompt raised *by the task we just sent* reported `blocked`,
  so a delivery that plainly landed was recorded `unknown`;
- input arriving mid-turn was queued by the CLI, which the pane reported as `idle`;
- every one of those left an `unknown` message that blocked all later messages
  until a human ran `resolve`.

Because a re-type could duplicate real work, the protocol forbade auto-resend, so
each failure needed a human. The cost was not the typing; it was that **delivery
was inferred from a TUI instead of being proven**.

## The change

The message becomes a file the crew reads. The terminal write becomes a doorbell
that carries no content and can be repeated safely.

```
captain assign/tell/answer ──▶ mail/<crew>/<id>.json ──▶ (doorbell) ──▶ crew pane
                                        ▲                                   │
                                        └──────── captain inbox ────────────┘
                                                  (read receipt)
```

Three consequences, all of which remove a class of bug rather than patch it:

1. **Delivery is proven, not inferred.** The crew's own `inbox` read writes a
   receipt, which carries the message from `sent` to the terminal `read` on the
   assignment the mail names. `sent` stops meaning "the pane looked right after
   we typed"; `read` means the crew has it.
2. **The doorbell is idempotent.** Re-ringing costs nothing, so a failed or
   uncertain ring is retried, not escalated at once. `pending` and `unknown`
   deliveries, the `cancelled` state and the `resolve` command that cleared them
   are gone with the keystroke transport; a delivery is `sent` then `read`.
3. **A blocked composer delays a nudge instead of losing a message.** The mail is
   already durable before the pane is touched.

Prior art: munder-difflin (`src/main/hive.ts`) routes agent-to-agent mail through
per-agent `inbox/`, `outbox/` directories and reduces the PTY write to a wake
nudge (`src/main/workerWake.ts`, `docs/message-queue.md`). We take that split and
almost nothing else: they run an Electron main process with a renderer drain loop;
we have no daemon, so our crew drain their own mail with a CLI command at the top
of a turn, and the only long-lived process is the `wait` the captain already runs.
That `wait` is also what retries a held ring, so arming one after recruiting is
required, not advisory: a crew nobody is waiting on is never rung, and a Codex or pi
crew can then sit on unread mail indefinitely. A Claude crew cannot: its own `Stop`
hook keeps the turn from ending, and reading its mail is what releases it.

## The mail

One directory per session, beside `protocol.json`:

```
sessions/<id>/mail/<crew>/<message-id>.json
```

A message is the record `deliver` already builds, plus a state and two stamps:

| Field | Meaning |
|---|---|
| `id`, `assignment_id`, `incarnation_id` | unchanged from today's message record |
| `kind` | `assignment`, `followup`, `answer` (unchanged) |
| `text` | the body the crew reads |
| `queued_at` | when the captain wrote it |
| `state` | `queued` → `read`, or `bounced` |
| `read_at` | set by the crew's `inbox` read |

Single writer per file, like their hive: the captain creates it, the crew's own
CLI marks it read. No lock beyond the existing session lock.

`state` deliberately has no `sent`. There is no step between writing the file and
the crew reading it that can half-succeed.

## The doorbell

`pane.nudge(crew)` types one fixed sentence: *read your mail with `captain inbox
<name>`*. It carries no task text, so a mangled ring is harmless.

It fires only when all of these hold. Their drain applies the same shape
(`docs/message-queue.md` §2, `workerWake.ts`); the values are ours:

| Gate | Why |
|---|---|
| unread mail exists whose head was not already rung | edge-triggered: `ring` records the message, the time and whether the nudge landed; a landed ring is repeated only on the slow retry that exists to bounce a pane that died after it. Their `announced` set |
| the pane is quiescent | don't interrupt a turn |
| past a boot grace window from launch | the CLI is still painting its banner |
| no native approval prompt is waiting | a human is deciding; typing into that is how we lost a crew today |
| no user draft on the composer | already implemented, `pane.draft_pending` |
| a cooldown since the last ring to this crew | two rings in a row jam a TUI |

A gate that does not clear is not an error. The mail stays queued, `status` says
which gate is holding it, and the next ring tries again. Nothing is lost by
waiting, which is the opposite of today, where a held composer loses the message.

Waiting forever is still a failure, though, so a hold is escalated rather than
forced: when the user-draft gate has held one message for
`protocol.HELD_NOTICE_INTERVAL` (300s), the next drain appends a single `held`
notice naming the crew, the gate and roughly how long, so the captain's next
`wait` surfaces it. Exactly one per message, recorded in the `.drain` stamp. The
nudge is still never typed over the draft and the draft is never cleared for us:
submitting a human's unsubmitted text is what this design exists to prevent. The
other gates need no escalation - an approval prompt already raises
`awaiting_approval`, and a busy agent is just working.

The ring is still only a request, so on Claude crew the turn itself is held open.
Their `Stop` hook no longer just records that the turn ended: while the crew has
unread mail, or while its active assignment is unfinished and no question is
pending, it returns a blocking stop decision, and the crew stays active instead of
ending a turn on a message it never read. Reading mail and finishing with `done`
stop being wording in the instructions that a crew can drift past. This is the
fallback for a crew that stops without reporting: `done` itself already refuses
while mail is unread, so a turn that ends in `done` surfaces the mail first and
the block never fires. A crew awaiting an answer to `ask` may stop, having
nothing to do until the captain replies. This is Claude-only - Codex's `notify`
and pi cannot block - and the hook fails open, so no crew is ever wedged by it.

## Signals count only after they are asked for

Every notification carries the `queued_at` of the message that prompted it, and a
crew signal older than the message it answers is ignored. That kills stale
notices by construction rather than by retiring them after the fact, and it
removes the race where a `wait` polls between "message written" and "send
finished" and calls a healthy send uncertain.

Their completion watcher does the same with a `dispatchedAt` stamp
(`src/main/realtimeCompletionWatcher.ts`).

## Undeliverable mail is reported, never parked

If a crew cannot be rung at all — its pane is gone, its agent is unregistered —
the mail is marked `bounced` with the reason, and the next `wait` returns it as a
notification naming the crew, the message and why. Their router bounces to the
god agent with the reason in the subject and logs delivery only for targets that
actually took it (`hive.ts`, `routeMessage`). We have one captain, so the bounce
goes to the captain's own notification stream.

## What we are deliberately not taking

- **A circuit breaker** (`src/main/breaker.ts`). Real problem, wrong time: we have
  no cost signal wired into `wait`, and the dashboard already shows spend.
- **`fleet.json`.** `captain status` plus the dashboard already answer it.
- **FIPA-style acts, conversations, broadcast, a hop cap.** One captain, disjoint
  file ownership, no crew-to-crew mail. A hop cap guards a loop we cannot form.
- **Their retry-by-typing and draft fusing.** Retrying a *doorbell* is safe;
  retyping a task is not, and typing after a user's draft to fuse with it is a
  trade only their transport forces.
- **A drain loop as a resident process.** No daemon is a project rule. The known
  cost stands, and the `Stop` hook does not pay it: `drain` runs only inside
  `poll`/`wait`, so a ring held when a wait returns stays invisible until the next
  wait begins, and there is no resident router.

## Seam

Two crews can build against this without touching the same file:

```
protocol.enqueue(crew, text, kind, assignment_id) -> message id   # writes the file
protocol.unread(crew) -> [message]                                # queued, oldest first
protocol.mark_read(crew, ids) -> None                             # receipt: read_at, sent -> read
protocol.bounce(crew, message_id, reason) -> None                 # notification
pane.nudge_block(crew) -> str | None                              # which gate holds, or None
```

`deliver` keeps its signature and becomes `enqueue` plus a best-effort ring, so
`assign`, `tell` and `answer` need no change at their call sites.

## Phasing

1. **Mail and receipts.** `protocol.py`, `cli.py` (`captain inbox NAME`), crew
   instructions gain one line: read your mail at the start of a turn. Tests cover
   a message surviving a refused ring, and a receipt proving delivery.
2. **Doorbell and gates.** `pane.py`, `agents.py`. Tests cover each gate holding
   the ring and none of them losing the message, plus bounce on a dead pane.
3. **Retire the workarounds.** Landed as a deletion once receipts existed: the
   `pending`/`unknown`/`cancelled` delivery states, `resolve`, and the
   stale-notice retirement are gone, with nothing left that can reach them.

Phase 3 only lands if phases 1 and 2 prove out on a real session.
