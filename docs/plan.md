**Current scope - implemented first slice**

The current implementation follows the Herdr-only flow in [README.md](../README.md).

- Run `captain` inside a Herdr workspace. Ask for Claude Code or Codex, rename its tab to **Captain Barbossa**, and replace the launcher with the selected native CLI.
- Ask the user for the agent and pane or tab for each crew. Use Herdr to create that topology and run a short session-local launcher there, then wait for the native agent to be ready before submitting its task.
- Focus existing crew by name with `captain focus Jack`, including natural-language navigation requests to the captain.
- Store graph memory outside the repo, separated by canonical project path and Captain session. Query it using the installed Graphify CLI.

There is no custom terminal UI, tmux layer, chat server, or background orchestration daemon.

**Done since**

- Crew signalling reads the native CLI's own hook events (Claude Code `--settings` hooks, Codex `notify`) from `sessions/<id>/events/<crew>.jsonl` with cursor files, instead of scraping panes. Pane detection remains only as a fallback while no event has arrived; Codex's title-generation turn is filtered out.
- Publishing: the package is on PyPI as `captain-barbossa`. `release.yml` publishes `X.Y.Z.devN` from `uat` to TestPyPI and `vX.Y.Z` tags to PyPI, through trusted publishing with per-ref GitHub environments.

**Open**

- A stale idle `Notification` can trip `wait`: an unconsumed "waiting for your input" event from the gap before a follow-up prompt is read as idle while the crew is working.

The native agents use the small CLI commands through their existing tools. Follow-up scheduling, automatic integration of parallel edits, and provider transcript resume are outside this first implementation. See the README for exact behavior, setup, memory retention, and verification limits.
