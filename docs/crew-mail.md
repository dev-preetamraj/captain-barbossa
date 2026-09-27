# Crew mail: move the message off the keystroke

Status: design, not yet built. Supersedes nothing; it changes how `assign`, `tell`
and `answer` reach a crew, and leaves the assignment protocol in
[crew-lifecycle.md](crew-lifecycle.md) intact.

## The problem this solves

Today a message to a crew *is* a terminal write. `deliver` persists the message,
then types it into the native CLI's composer and infers from the pane whether it
landed. Every quirk of that composer is therefore a delivery failure:

- a contextual suggestion (`Try "refactor agents.py"`) reads as a user draft, so
  the send is refused;
- a native permission prompt raised *by the task we just sent* reports `blocked`,
  so a delivery that plainly landed is recorded `unknown`;
- input arriving mid-turn is queued by the CLI, which the pane reports as `idle`;
- every one of those leaves an `unknown` message that blocks all later messages
  until a human runs `resolve`.

Because a re-type could duplicate real work, the protocol forbids auto-resend, so
each failure needs a human. The cost is not the typing; it is that **delivery is
inferred from a TUI instead of being proven**.

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
   receipt. `sent` stops meaning "the pane looked right after we typed".
2. **The doorbell is idempotent.** Re-ringing costs nothing, so a failed or
   uncertain ring is retried instead of escalated. `unknown` deliveries, and the
   `resolve` command that exists to clear them, lose their reason to exist for
   ordinary sends.
3. **A blocked composer delays a nudge instead of losing a message.** The mail is
   already durable before the pane is touched.

Prior art: munder-difflin (`src/main/hive.ts`) routes agent-to-agent mail through
per-agent `inbox/`, `outbox/` directories and reduces the PTY write to a wake
nudge (`src/main/workerWake.ts`, `docs/message-queue.md`). We take that split and
almost nothing else: they run an Electron main process with a renderer drain loop;
we have no daemon, so our crew drain their own mail with a CLI command at the top
of a turn, and the only long-lived process is the `wait` the captain already runs.
That `wait` is also what retries a held ring, so arming one after recruiting is
required, not advisory: a crew nobody is waiting on can sit on unread mail indefinitely.

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
| unread mail exists whose ids were not already announced | edge-triggered: undrained mail is never re-announced on a timer. Their `announced` set |
| the pane is quiescent | don't interrupt a turn |
| past a boot grace window from launch | the CLI is still painting its banner |
| no native approval prompt is waiting | a human is deciding; typing into that is how we lost a crew today |
| no user draft on the composer | already implemented, `pane.draft_pending` |
| a cooldown since the last ring to this crew | two rings in a row jam a TUI |

A gate that does not clear is not an error. The mail stays queued, `status` says
which gate is holding it, and the next ring tries again. Nothing is lost by
waiting, which is the opposite of today, where a held composer loses the message.

## Signals count only after they are asked for

Every notification carries the `queued_at` of the message that prompted it, and a
crew signal older than the message it answers is ignored. That kills stale
notices by construction rather than by retiring them after the fact, and it
removes the race where a `wait` polls between "message written" and "send
finished" and reports `delivery_unknown` for a healthy send.

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
- **A drain loop as a resident process.** No daemon is a project rule.

## Seam

Two crews can build against this without touching the same file:

```
protocol.enqueue(crew, text, kind, assignment_id) -> message id   # writes the file
protocol.unread(crew) -> [message]                                # queued, oldest first
protocol.mark_read(crew, ids) -> None                             # receipt, sets read_at
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
3. **Retire the workarounds.** Once receipts exist: no `unknown` state for an
   ordinary send, `resolve` narrows to genuine terminal accidents, and the
   stale-notice retirement added earlier can go.

Phase 3 only lands if phases 1 and 2 prove out on a real session.
