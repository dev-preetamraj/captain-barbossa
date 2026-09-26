# Crew lifecycle

## Recruiting crew

Ask the captain to spin up a crew in plain language; its startup instructions
carry a recruiting ruleset. When you state no preference it recruits with no
questions, using:

- **agent**: the CLI the captain itself runs as
- **placement**: a pane split picked from the tab layout (auto), or a new tab
  when crowded
- **model**: the `cheap` tier, stepped up only for genuinely harder work

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
assigned in that order and skipping names already in use. Reusing a protocol
crew's retired name requires `crew NAME --handoff <old-assignment-id>` after
completion and acknowledgement; automatic selection does not supply that handoff.
Once every name is taken, numbering starts at Jack-2. Barbossa is
reserved for the captain. The same name is used everywhere: the crew ID, the
pane/tab label, and the name in memory, `wait`, `focus`, `model`, and `dismiss`.

New crew panes/tabs open in the same workspace and project without stealing
focus, and the task is submitted once the native agent is ready. A task that
never starts, or an agent waiting for approval, preserves the pane for
inspection. Protocol delivery is attempted once; uncertain delivery must be
inspected and explicitly resolved before sending another message.
The startup launcher deletes itself before starting the native CLI and removes
its path from that process's environment. Crew may inspect project files and Git,
read repo state, add session memory, and use `ask`, `done`, and `check` for their
own assignment. `memory show` is forced to repo scope. Crew cannot inspect other
session/project state or recruit, answer, resolve, reassign, or dismiss crew.

Placement flags and tab shapes are documented in
[placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md).
Model tiers and defaults are documented in
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md).

## Assignments and questions

New recruits receive separate crew-incarnation, assignment, and message IDs.
`crew --owns PATH --allow ACTION` records ownership and grants; repeat either
flag as needed. `read` and `search` are always granted. Other actions are `edit`,
`test`, `build`, `format`, `commit`, `version`, and `delete`. Overlapping active
ownership is refused, and `check` requires owned paths for mutation actions.
These checks do not intercept native tools or grant sandbox/user approval.

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
an answered question, and no uncertain prompt delivery. Reassignment requires
`done`, acknowledgement of prior notifications, and the exact handoff ID.
Stale assignment, incarnation, and question IDs are refused.

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
captain wait Jack --json
captain wait Jack --json --ack <delivery-id> --timeout 0
```

For protocol crew, `wait` returns a notification with `status`, `delivery_id`,
`crew`, `assignment_id`, and a summary capped at 1,600 characters. `--json`
selects JSON output. Statuses distinguish `working`, `idle`, `asked`,
`awaiting_approval`, `done`, `delivery_unknown`, `timeout`, and `error`. Native
idle is activity evidence only; assignment completion requires explicit `done`.
Approval notifications never approve the underlying native prompt.

A notification repeats with the same delivery ID until `wait --ack ID`
acknowledges it. Acknowledging may return the next notification; consume that
too. Timeouts have no delivery ID and do not imply completion. `--timeout 0`
polls once; the default is 900 seconds. Neither waiting nor timing out retries
a terminal prompt. After inspecting an uncertain prompt, use
`resolve NAME MESSAGE_ID sent|cancelled --assignment ID`; this records the
observed outcome and sends no input.

Legacy crew retain the older non-JSON wait below; they are not silently adopted
into the protocol. Recruit new crew to use acknowledged notifications.

The final status and the crew's own report are printed and recorded in memory,
falling back to the crew's last message or the tail of its pane when it
reported nothing. A crew still working when the timeout (900s by default)
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
conversation are kept. For protocol crew, `--assignment` is required and the
message is appended without replacing the original task, ownership, or grants.
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
about 60 characters. Status is refreshed live from Herdr for each crew,
falling back to the last recorded status if Herdr cannot be reached. Dismissed
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
conversation, and the assignment are untouched. Claude Code's inline `/model`
also saves the model as the default for new sessions, and the command prints
that as a reminder.
If the installed native picker does not offer the requested model, the switch
fails; a tier in Captain's catalog does not guarantee native availability.

## Dismissing crew

```sh
captain dismiss Jack
```

This closes the crew's pane, retires the name (freeing it for reuse), and
records the dismissal in memory. Protocol crew must first finish with a report
and have all notifications acknowledged. It is permanent, so handle unreported
or uncommitted work first; commit only when the user explicitly requested it.

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

Assignment records reject overlapping declared ownership and `check` validates
grants, but nothing locks files or intercepts arbitrary native tools. Keep the
native sandbox enabled; these commands are not broad native allow rules.
