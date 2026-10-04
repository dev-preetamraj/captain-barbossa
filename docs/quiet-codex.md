# Codex and quiet turns

Codex supports headless operation with a read-only gate; `captain quiet` runs it with
no pane and fully determined flags.

## Headless invocation

```text
codex exec --json \
  --ignore-user-config \
  --skip-git-repo-check \
  --ephemeral \
  --sandbox read-only|workspace-write \
  [--model MODEL] \
  TASK
```

The `--ignore-user-config` flag is part of the argv, not an optional override. See
[Finding 1](#finding-1-sandbox-defeat-through-user-config) below.

## Result reader

Codex writes JSONL: one event per line. A successful quiet turn ends with a
`turn.completed` item, which records spend. The result text comes from the last
`agent_message` item in the stream.

If `turn.completed` does not arrive or the message list is empty or the last message
is blank, the turn is incomplete.

Codex narrates before it answers: "I'll create the file..." then "DONE". The reader
takes the last message, so a file-creation turn reads as success when it actually
succeeded, and failure when it did not.

Spend is reported as `output_tokens` only; total spend is not written.

## Write gate

`--sandbox workspace-write` allows edits. Writes are recorded (a file created stays
created), and the same result-reading rules apply.

## Finding 1: sandbox defeat through user config

The first live run created a file anyway despite `--sandbox read-only`. The cause: a
user's own `~/.codex/config.toml` can hold `approvals_reviewer = "auto_review"` and a
list of trusted projects, either of which silently grants write approval and defeats
the sandbox.

Auth still comes from `CODEX_HOME`, so `--ignore-user-config` is safe. With this flag,
the sandbox holds. It must be part of the argv (not an option the captain decides per
call), because a gate that a user's own config can silently turn off is not a gate.

Verified live against codex-cli 0.158.0: the write was blocked only with
`--ignore-user-config`, and the success read worked.
