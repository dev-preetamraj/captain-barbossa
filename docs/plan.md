**Current scope — implemented first slice**

The current implementation follows the Herdr-only flow in [README.md](../README.md).

- Run `captain` inside a Herdr workspace. Ask for Claude Code or Codex, rename its tab to `captain barbossa`, and replace the launcher with the selected native CLI.
- Ask the user for the agent and pane or tab for each crew. Use Herdr to create that topology and run a short session-local launcher there, then wait for the native agent to be ready before submitting its task.
- Store graph memory outside the repo, separated by canonical project path and Captain session. Query it using the installed Graphify CLI.

There is no custom terminal UI, tmux layer, chat server, or background orchestration daemon. The earlier images and [original plan](archive/plan-v1.md) are historical design artifacts, not current requirements.

The native agents use the small CLI commands through their existing tools. Follow-up scheduling, automatic integration of parallel edits, and provider transcript resume are outside this first implementation. See the README for exact behavior, setup, memory retention, and verification limits.
