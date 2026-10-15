# Troubleshooting

- **`captain: command not found`** — run `uv tool update-shell` and restart
  the terminal.
- **Installed from Git before the PyPI release** — switch the install over
  once with `uv tool install --force captain-barbossa`; `captain update`
  then picks up each published release.
- **"Launch captain from an interactive Herdr terminal."** — `captain` with
  no subcommand needs a TTY; run it directly in a Herdr pane, not through a
  script or pipe.
- **Crew pane opens but the task never starts, or is marked
  `needs_attention`** — the native CLI may be waiting for approval or
  sign-in. Inspect the pane in Herdr; the task is not retried automatically
  past one resend. Send it by hand with
  `herdr agent prompt <agent-name> '<task>'`.
- **A Claude Code crew will not end its turn** — its `Stop` hook blocks while
  the crew has unread mail, or an unfinished assignment with no pending
  question. Read the mail with `captain inbox <name>` in that pane, finish with
  `captain done <name> --report '...'`, or raise a question with `captain ask`;
  any of the three releases it.
- **Codex crew blocked on approval is invisible to `captain wait <crew>
  --timeout 0`** — pane fallback starts after 6 seconds; use a non-zero
  timeout.
- **"... is waiting for input or approval instead of starting the task."** —
  inspect the native prompt. Claude PreToolUse hooks allow reads and scoped
  known requests, deny ungranted actions, and leave anything they cannot
  classify to the native permission mode. Only destructive, outward (network,
  push, publish) or out-of-scope writes ask the user; relay those instead of
  typing an approval. Codex crew launch with `--sandbox workspace-write
  --ask-for-approval on-request` and grok crew with `--permission-mode auto`;
  judge their prompts by the same rules.
- **"... did not confirm the switch to ..."** — the native CLI did not echo
  the expected model name; read the pane with `herdr agent read <name>`
  before retrying `captain model`.
- **Crew cannot see my dirty/staged/untracked files** — supported Git projects
  default to a fresh detached worktree at the captain checkout's local `HEAD`; those
  parent working-tree inputs are intentionally absent. Commit or explicitly transfer
  the inputs first, or recruit with `--checkout shared` when shared-root operation is
  intended.
- **`[unsupported-checkout]` / isolated crew refuses this repository** — worktree
  isolation currently requires an ordinary Git checkout whose `.git` is a directory.
  Linked-worktree captain checkouts, symlinked roots, alternates, partial/promisor
  stores, and other unsupported configurations are retained/refused rather than
  normalized. Use an ordinary checkout, or explicitly choose `--checkout shared` if
  its collision risk is acceptable. An explicit `--checkout worktree` also refuses a
  non-Git project; an omitted mode falls back to shared there.
- **`result apply` reports overlap, unresolved content, or an existing/incomplete
  transfer** — nothing was staged or committed. Inspect `captain result show ID` and
  the destination index/working files. Resolve manually without discarding journals;
  apply intentionally refuses unfinished Git operations, changed preimages,
  symlinks/submodules, out-of-grant content, and partial/external modifications.
- **`worktree cleanup` reports `[cleanup-pending]` or retains the checkout** — cleanup
  needs authoritative managed-writer exit evidence, a final unchanged result, and a
  verified apply or explicit `result discard --reason ...`. A busy/unknown writer,
  failed terminal close, detached commit, distinct staged blob, conflict, or partial
  transfer is retained for review. Repeating a caller claim that work stopped does not
  bypass the lifetime receipt.
- **Native history says `pending`, `partial`, or `unsupported`** — Captain freezes
  usage and deletes only locally recorded identities owned by that exact crew
  incarnation after the lifetime check. Missing manifests cause no deletion; missing
  hooks, unobserved or resumed identity, missing executables, unsupported providers,
  shared sidecars, and partial failures stay reported. Retained non-complete journals
  block pruning. Cleanup never broadens to captain or unrelated histories; retain the
  session and retry only after fixing the stated cause.
- **"This session belongs to another project or Herdr workspace."** — a
  session ID is tied to the captain checkout/project and workspace it was created in;
  an isolated crew execution root does not change that identity. Start a new captain
  or pass the matching `--session`.
- **"durable memory root ... is inside the OS temp directory"** — a captain
  started before the state/temp split is still exporting one root to its
  children; restart it so project-scope memory survives temp cleanup.
- **`memory query` fails or reports a skip** — Graphify is not installed; run
  `uv tool install graphifyy`, or use `memory show`/`add` instead.
- **Ambiguous crew name or model** — the error lists the available crew or
  models; ask for one of those exactly.
