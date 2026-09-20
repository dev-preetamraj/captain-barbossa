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
assigned in that order and skipping names already in use. Dismissing crew
frees the name for reuse, so the next recruit takes the lowest free roster
name again. Once every name is taken, numbering starts at Jack-2. Barbossa is
reserved for the captain. The same name is used everywhere: the crew ID, the
pane/tab label, and the name in memory, `wait`, `focus`, `model`, and `dismiss`.

New crew panes/tabs open in the same workspace and project without stealing
focus, and the task is submitted once the native agent is ready. A task that
never starts, or an agent waiting for approval, preserves the pane for
inspection and reports an error naming the `herdr agent prompt` command to
send the task by hand; nothing is retried automatically beyond one resend.

Placement flags and tab shapes are documented in
[placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md).
Model tiers and defaults are documented in
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md).

## Waiting for crew to finish

```sh
captain wait Jack
captain wait Jack --timeout 300
```

`wait` blocks until the crew is done, idle, or blocked. It follows the native
CLI's own lifecycle events rather than reading its pane, so a pause between
tools is not mistaken for the end.

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

`tell` prompts an existing crew in place; its pane, model, and running
conversation are kept. The message replaces the crew's recorded assignment
and is saved to memory, and any idle event left over from before the prompt is
consumed first, so the next `captain wait Jack` reports the new work rather
than the old pause. Dismissed crew are refused; recruit new crew instead.

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
reports an error and changes nothing. Codex keeps the reasoning level it
already had. A confirmed switch updates the session and memory; the pane, the
conversation, and the assignment are untouched. Claude Code's inline `/model`
also saves the model as the default for new sessions, and the command prints
that as a reminder.

## Dismissing crew

```sh
captain dismiss Jack
```

This closes the crew's pane, retires the name (freeing it for reuse), and
records the dismissal in memory. It is permanent, so confirm any unreported
or uncommitted work is handled first: crew commit their own hunks, and the
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
