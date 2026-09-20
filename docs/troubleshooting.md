# Troubleshooting

- **`captain: command not found`** — run `uv tool update-shell` and restart
  the terminal.
- **Installed from Git before the PyPI release** — switch the install over
  once with `uv tool install --force captain-barbossa`; `uv tool upgrade`
  then picks up each published release.
- **"Launch captain from an interactive Herdr terminal."** — `captain` with
  no subcommand needs a TTY; run it directly in a Herdr pane, not through a
  script or pipe.
- **Crew pane opens but the task never starts, or is marked
  `needs_attention`** — the native CLI may be waiting for approval or
  sign-in. Inspect the pane in Herdr; the task is not retried automatically
  past one resend. Send it by hand with
  `herdr agent prompt <agent-name> '<task>'`.
- **Codex crew blocked on approval is invisible to `captain wait <crew>
  --timeout 0`** — pane fallback starts after 6 seconds; use a non-zero
  timeout.
- **"... is waiting for input or approval instead of starting the task."** —
  read the pane before approving, then send the requested key with
  `herdr agent send-keys <name> <key>` (Claude Code may need Enter or a
  number, not always `y`).
- **"... did not confirm the switch to ..."** — the native CLI did not echo
  the expected model name; read the pane with `herdr agent read <name>`
  before retrying `captain model`.
- **"This session belongs to another project or Herdr workspace."** — a
  session ID is tied to the project and workspace it was created in; start a
  new captain or pass the matching `--session`.
- **"durable memory root ... is inside the OS temp directory"** — a captain
  started before the state/temp split is still exporting one root to its
  children; restart it so project-scope memory survives temp cleanup.
- **`memory query` fails or reports a skip** — Graphify is not installed; run
  `uv tool install graphifyy`, or use `memory show`/`add` instead.
- **Ambiguous crew name or model** — the error lists the available crew or
  models; ask for one of those exactly.
