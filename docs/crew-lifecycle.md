# Crew lifecycle

## Recruiting crew

Ask the captain to spin up a crew in plain language; its startup instructions
carry a recruiting ruleset. When you state no preference it recruits with no
questions. The command resolves omitted flags, using:

- **agent**: `[crew] agent`, or the CLI the captain itself runs as
- **placement**: `[crew] placement`, or the next declared slot (auto), opening
  a new tab when the existing shapes are full
- **model**: `[crew] model` (`cheap` as shipped), stepped up for harder work

Explicit flags win; split flags imply pane placement. The command prints the
source of agent, model and placement. Only a missing agent in both settings and
captain metadata needs a picker or `--agent` in a non-TTY.

Every choice you do state is used as given; the captain asks at most one
question, and only when you hand a choice back to it ("ask me where to put
it") or name one too vaguely to map to a flag. Only the captain recruits;
crew forward any delegation request back to the captain instead of spawning
their own.

You can also run the command directly from a shell attached to the captain's
session:

```sh
captain crew --task "Review the current changes"
captain crew gibbs --task "Review the current changes"
```

Every crew gets a one-word Pirates of the Caribbean name: Jack, Will,
Elizabeth, Gibbs, Anamaria, Pintel, Ragetti, Cotton, Marty, Tia, Davy, or Sao,
assigned in that order and skipping names already in use. A dismissed crew's
name returns to the pool automatically; the next recruit, automatic or named,
gets it back with a fresh assignment. `crew NAME --handoff <old-assignment-id>`
is only needed to carry the old assignment forward instead of starting fresh.
Once every name is taken, numbering starts at Jack-1. Barbossa is
reserved for the captain. The same name is used everywhere: the crew ID, the
pane/tab label, and the name in memory, `wait`, `focus`, `model`, and `dismiss`.

New crew panes/tabs open in the same workspace and project without stealing
focus, and the task is submitted once the native agent is ready. A task that
never starts, or an agent waiting for approval, preserves the pane for
inspection. Protocol delivery is mail plus one doorbell: the message is durable
before the pane is touched, so it is queued for the crew to read or bounced.
The startup launcher deletes itself before starting the native CLI and removes
its path from that process's environment. Crew may inspect project files and Git,
read repo state, add session memory, and use `ask`, `done`, and `check` for their
own assignment. `memory show` is forced to repo scope. Crew cannot inspect other
session/project state or recruit, answer, reassign, or dismiss crew.

Placement flags and tab shapes are documented in
[placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md).
Model tiers and defaults are documented in
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md).

## Shared checkout and session cleanup

Crew share the captain working tree, including dirty, staged, and untracked files.
Give writers disjoint owned paths and serialize same-file work. Overlapping active
assignments fail with `[owned-path-conflict]`.

Before targeted native-history deletion, cleanup verifies authoritative writer shutdown and freezes
usage. The ownership manifest is crew-only and incarnation-scoped: only locally recorded
session IDs/transcripts are candidates, never the captain's or unrelated histories.
Missing manifests or hooks, unobserved/resumed/ambiguous identity, unknown writers,
missing provider executables, unsupported providers, partial deletion, and shared
history/sidecars remain `pending`, `partial`, or `unsupported`. They are reported with
no broad deletion; retained non-complete history journals block pruning.

## Background crew

`crew --background` recruits a crew with no pane for one read-only headless turn.
It has the same name, incarnation, assignment, read/search grants and acknowledged
deliveries as any crew; only execution differs. Agent and model resolve as above,
with placement skipped; Herdr is never called to launch it. It shares the captain checkout;
the provider-specific read-only gate remains authoritative. There is no PTY, persistent
daemon, worker pool, or automatic staging/commit.

Lifecycle:

1. Recruitment refuses placement flags, `--owns`, `--handoff`, write grants, a
   non-finite `[crew] background_timeout`, and the `claude` and `grok` agents (a
   headless claude turn still runs the user's hooks and settings allow rules; grok
   documents no flag to drop its MCP servers and plugins) before reserving
   anything, and refuses a legacy temp-root session. codex and pi are supported.
2. The roster entry (`status: starting`) is written before the assignment is
   reserved, and the run record and intent before any process starts, so a
   recruiter killed at any step leaves a crew that `status` shows (`state:failed`
   when nothing ran), the next `wait` reconciles, and `dismiss` retires; its name
   stays taken until then. From before the assignment is reserved until the owner
   claims or the recruiter withdraws, the recruiter holds a lock that tells
   `status` and `wait` it is still deciding. A detached owner then starts. Claim and
   withdrawal are each one rename of the same intent file, so exactly one wins,
   atomically and with no lock a stopped process could hold: recruitment returns
   once the owner claims, or after 10 seconds withdraws the intent (`abandoned`),
   closes the assignment as `[background-start]` (waiting at most 2 seconds for the
   protocol lock, else leaving that to the next `wait`) and frees the name. An owner
   that resumes later finds the intent gone and runs nothing; a claim made more than
   20 seconds after the intent, or for an assignment already closed, withdraws too.
   That clock only expires stale intent; whether a recruiter or owner is alive is
   always decided by its lock, never by time or a PID.
3. The owner runs the provider's read-only headless argv (quiet's gate plus pi's
   isolation flags in the README) under a guard process that leads its own process
   group, with stdout capped at 8 MiB and stderr at 64 KiB, until the CLI exits,
   `[crew] background_timeout` passes, the output cap is hit, or a cancel is
   requested. The guard starts watching the owner before it starts the CLI, keeps
   running through TERM, and after the CLI exits reports its status and waits to be
   killed, so it is the group's last member until the owner's KILL. However the run
   ends, the owner sends the group TERM, then KILL after up to 5 seconds of grace
   (none after a clean exit), and only then reaps the guard; a descendant that
   ignored TERM or still held the output pipes is gone. Only a process that left
   the group (`setsid`, daemonizing, its own process group) can outlive the run; the
   owner waits 5 seconds more for output such a process holds and moves on.
4. A complete result document becomes `done` with the report (at most 4000
   characters in the delivery; the whole report is kept beside the run record).
   Anything else closes the assignment with an `error` delivery:
   `[background-failed]`, `[background-timeout]`, `[background-output-limit]` or
   `[background-cancelled]`. `status` shows those as `state:failed`, never done.
5. A free lock with no recorded outcome means the owner was lost. The guard holds a
   pipe only the owner writes to, so the owner's death, however and whenever it
   happens (before the CLI starts, after it exits, or between TERM and KILL), closes
   it and the guard kills the whole group. `status` then shows `state:failed` with
   `[background-lost]`, and the next `wait` records and delivers that. Nothing is
   restarted.

`interrupt NAME` writes a cancel request and waits up to 10 seconds for the owner to
confirm it, reporting `cancelled` only when the cancel took effect and otherwise the
outcome that won. Only the owner signals the native CLI's group, before reaping its
leader, so a recycled id cannot be hit. With no owner running, nothing is signalled.
`wait NAME`, `wait --all` and `interrupt NAME` with an explicit `--session` resolve
no Herdr pane when every crew they reach is a background crew; involving a visual
crew they still need Herdr and fail clearly without it. `tell`, `answer`,
`assign --handoff`, `focus` and `model` are refused as `[background-unsupported]`.
`dismiss` needs the outcome acknowledged, like any crew, and closes no pane.

A pi captain arms a watch for every unresolved background crew when its session
starts, reloads or is reopened, rechecks every 5 seconds, and stops once the
outcome is acknowledged. A watch cancelled with `captain_wait` `cancel:true`, or one
that stopped on an error, is not rearmed by that sweep until it is armed again by
hand or the session starts again. Delivery is the existing bridge: written to the session
journal before sending and acknowledged only after it appears in pi's history.
Other captains arm `wait` as they do for any crew.

## Assignments and questions

New recruits receive separate crew-incarnation, assignment, and message IDs.
`crew --owns PATH --allow ACTION` records ownership and grants; repeat either
flag as needed. `read` and `search` are always granted. Other actions are `edit`,
`test`, `build`, `format`, `commit`, `version`, and `delete`. Overlapping active
ownership is refused, and `check` requires owned paths for mutation actions.
These checks do not intercept all native tools. Claude PermissionRequest hooks
add assignment-scoped approval decisions; other providers retain native permissions.

Here `check`, `ask`, and `done` run in Jack's new crew environment; `answer`
and `assign` run as captain:

```sh
captain check Jack edit src/example.py
captain ask Jack "Which behavior is intended?"
captain answer Jack <question-id> "Keep existing behavior" --assignment <assignment-id>
captain done Jack --report "Changed example.py; focused checks pass"
captain assign Jack --task "Next task" --handoff <old-assignment-id> \
  --owns docs/example.md --allow edit
```

New launches export `CAPTAIN_ASSIGNMENT`, letting crew omit `--assignment` on
`ask`, `done`, and `check`. The crew name, incarnation, and active assignment
must match; missing or stale context fails. Captain calls still require explicit
IDs, and replacement assignments require their new explicit `--assignment ID`.
Crew commands also validate `--incarnation`, defaulting to the launcher's
`CAPTAIN_INCARNATION`. Only one question may be pending. `done` requires a report,
an answered question, and no unread mail.
Reassignment requires `done`, acknowledgement of prior notifications, and the
exact handoff ID. Stale assignment, incarnation, and question IDs are refused.

`assign`, `tell`, and `answer` reach a crew as mail.
Each writes the message to a durable file, then rings the pane. The first ring
for that message is a Herdr agent prompt carrying the mail body plus one line
to run `captain inbox NAME`. A later ring after that first one landed is the
inbox line alone. `captain inbox NAME` prints that crew's queued mail oldest
first and writes the read receipt, which is what proves delivery and moves that
message from `sent` to `read` on its own assignment. `done` refuses while mail
is unread. For Claude Code crew that reading is also
enforced by the CLI's own `Stop` hook, which blocks the end of a turn while mail
is unread, or while the active assignment is unfinished with no question pending;
a crew waiting on an answer to `ask` may still stop. Codex and pi cannot block, so
for them reading mail and finishing with `done` remain instructions. See
[crew-mail.md](crew-mail.md) for the doorbell's gates and bounce handling.

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
captain wait Jack --json
captain wait Jack --json --ack <delivery-id> --timeout 0
captain wait --all --json --timeout 0
```

For protocol crew, `wait` returns a notification with `status`, `delivery_id`,
`crew`, `assignment_id`, and the full, untruncated summary. `--json`
selects JSON output. Statuses distinguish `working`, `idle`, `asked`,
`awaiting_approval`, `done`, `held`, `bounced`, `timeout`, and `error`. Native
idle is activity evidence only; assignment completion requires explicit `done`.
Approval notifications never approve the underlying native prompt. Claude's
PermissionRequest hook passes the tool, command/paths, cwd, and active assignment
grants to `approval.decide`. An allow-once returns a native `allow` decision with
no permission updates; deny returns `deny` and its reason. Both are logged as
PermissionDecision so they do not leave a pending approval notification.
Escalation (destructive verbs, network/push/publish, writes outside the owned
paths) logs the reason code, displays a user message, and leaves the native
prompt and wait notification pending for user review. Reads pass wherever they
point. A request the policy cannot classify, including missing or malformed input
and unreadable grants, gets no hook decision, so the native permission mode
decides as if no gate ran; stale incarnation hooks are ignored.
The captain must relay escalations to the user, not type an approval over them.
See the [native hook contract](https://code.claude.com/docs/en/hooks#permissionrequest-decision-control).

`wait` returns only when a notification carries a delivery ID. Native `working`
never notifies, and a native idle notifies at most once per message delivered, so
quiet work runs the timeout out instead of waking the captain every turn. Claude
Code crew are read from their event file alone, since their approvals arrive as
hooks; Codex and pi crew, whose CLIs report no approval, also have their pane
checked for a modal, once when a wait starts and then every 30 seconds.

A notification repeats with its existing delivery ID until acknowledged.
`wait --all` returns every protocol crew's current delivery in a `deliveries` array
with JSON, or prints each delivery in text. It scans the whole fleet before returning;
quiet, legacy, dismissed and fully acknowledged completed crew add no delivery.
It never acknowledges: use `wait NAME --ack ID` for each received delivery.
Each crew exposes one pending delivery at a time; queued notices follow its ack.
A fleet timeout returns an empty array. Acknowledging may return the next notification; consume that
too. `wait --ack ID` on the final `done` notification returns one `idle` status
instead of the next notification. Waiting again once the assignment is done and
every notification is acknowledged fails: dismiss the crew or hand off. Timeouts
have no delivery ID and do not imply completion. `--timeout 0` polls once; the
default is 86400 seconds. Neither waiting nor timing out retries a terminal prompt.
Mail is durable before the pane is touched, so a delivery is never uncertain: it
is either queued for the crew to read, or `bounced` with its reason.

Legacy crew retain the older non-JSON wait below; they are not silently adopted
into the protocol. Recruit new crew to use acknowledged notifications.

The final status and the crew's own report are printed and recorded in memory,
falling back to the crew's last message or the tail of its pane when it
reported nothing. A crew still working when the timeout (86400s by default)
expires records nothing and reports an error; wait again, or read its pane
directly with `herdr agent read <name>`.

For **legacy pi** crew the pane tail is written to `tail-<name>.txt` in the session
directory and the wait prints that path instead of the tail itself. pi installs
no lifecycle hooks, so legacy pi waits fall back to the tail; filing it keeps
each delivery short. Read the file when the status and report leave you
unsure. Claude Code and Codex crew, which do have hooks, still print the tail
inline on the rare legacy wait that has no event.

The pi captain's `captain_wait` tool instead arms a background JSON watch
(minimum 60 seconds) and returns immediately. It records delivery receipts and
checks pi session history before acknowledging ingestion; uncertain ingestion
stops the watch without resend or acknowledgement. Idle/working updates do not
trigger a new turn, and the watch remains armed until expiry or explicit cancel.
This is a local bridge, not a guarantee of exactly-once provider execution.

## Sending a follow-up

```sh
captain tell Jack "also update the changelog" --assignment <assignment-id>
```

`tell` prompts an existing crew in place; its pane, model, and running
conversation are kept. A message whose text repeats the last message on the
assignment is refused by that message's ID. For protocol crew,
`--assignment` is required and the message is appended without replacing the
original task, ownership, or grants.
Use `answer` for a pending question and `assign --handoff` after completion.
Legacy crew still replace their recorded task and advance the old event cursor.
Dismissed crew are refused; recruit new crew instead.

## Checking crew status

```sh
captain status
captain status --all
```

`status` prints a plain-text table of this session's crew: name, provider,
model, pane, status, and the first line of their assigned task, truncated to
about 60 characters. `Crew.current_state()` prints `state:… source:… detail`
from current hook, pane, or assignment evidence. Missing evidence stays `unknown`;
native idle or done alone does not establish assignment completion. Dismissed
crew are omitted unless `--all` is given; `captain status` with no crew prints
`No crew.`

## Focusing crew

Tell the captain "focus on Jack", "switch to Will", or "take me to
Elizabeth", or run:

```sh
captain focus Jack
```

Names are case-insensitive; crew IDs and Herdr agent names also work.
Focusing switches to the crew's tab first when it differs from the captain's,
follows the registered agent if its pane has moved, and never sends input or
interrupts its work. An unknown or ambiguous name reports the available
choices.

## Switching a running crew's model

```sh
captain model Jack strong
captain model Will cheap
```

This drives the CLI's own `/model` command through Herdr and verifies the
result: without the CLI's own confirmation line naming that model, the command
reports an error without recording a successful switch. Codex keeps the reasoning level it
already had. A confirmed switch updates the session and memory; the pane, the
conversation, and the assignment are untouched. The switch stays inside the
crew's session: named inline, Claude Code's `/model` would also save the model
as the user's default for new sessions, so the retier opens its picker instead
and takes the key that switches this session alone. The picker opens focused on
the model in use, so focus is moved one keypress at a time, and the key is
pressed only on a row the pane has just shown as focused and labelled exactly
the model asked for. A version is part of that label: a request for Opus 5 never
takes an Opus 5.5 row. Focus that stops moving, or never reaches the row,
refuses naming the row it stopped on, having selected nothing.
If Claude Code then asks to confirm the switch, because it re-reads the
conversation, the confirm is answered only when it names the model asked for.
A switch whose confirmation says it was saved as the default is refused, since
that confirm step can persist the default.
If the installed native picker does not offer the requested model, the switch
fails; a tier in Captain's catalog does not guarantee native availability.

## Dismissing crew

```sh
captain dismiss Jack
```

This closes the crew's pane, retires the name (freeing it for reuse), and
records the dismissal in memory. Protocol crew must first finish with a report
and have all notifications acknowledged; a refusal names whichever of those, or
an unresolved delivery, is actually holding it. Any mail still unread at that
point is bounced with a reason instead of being left queued, and the command
names each bounced message. It is permanent, so handle unreported or
uncommitted work first; commit only when the user explicitly requested it.

## Editing guardrails

Shared-checkout crew can collide directly. Every crew carries this contract:

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

Assignment records reject overlapping declared ownership and `check` validates
grants, but nothing locks files or intercepts arbitrary native tools. Keep the
native sandbox enabled; these commands are not broad native allow rules.
