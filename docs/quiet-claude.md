# Claude: headless quiet turn

A quiet turn for Claude runs with a headless mode flag, a permission gate per sandbox level, and session-directory access for reading assignment and mail state.

## Command-line interface

The literal argv for a read-only or writable Claude turn, built by `models.headless_argv` and read by `models.headless_report`:

| Layer | Setting |
| --- | --- |
| headless mode | `-p TASK --output-format json` |
| permissions | `--permission-mode dontAsk` |
| read-only gate | `--allowedTools Read Grep Glob` + `--disallowedTools Bash Write Edit` |
| write gate | `--allowedTools Read Grep Glob Edit Write` (no denylist) |
| session directory access | `--add-dir <path>` (read-only turns only) |
| model selection | `--model <name>` (when set) |

Read-only turns get `--add-dir` so they can read the event log, mail and assignment records without pasting them into the prompt. Write turns get neither the flag nor the directory path, confining unwatched edits to the named files and the worktree diff.

## Result format

Claude returns a single JSON object:

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "text output",
  "num_turns": 1,
  "total_cost_usd": 0.0001
}
```

A successful turn has `type=result`, `subtype=success`, and `is_error=false`. The text is in the `result` field. Spend is reported as `num_turns` and `total_cost_usd`.

Any other shape - a wrong `type`, `subtype`, or `is_error=true`, or a missing `result` string - is treated as failure and the turn is rejected as incomplete.

## Design notes

The headless prompt leads the task text rather than using a system-prompt flag, so three sentences at the top of `--task` set the tone and mode. No fourth code path for a provider-specific flag is needed.

The permission gate is Claude's strongest: it names what the turn is allowed to do rather than blocking specific actions. The write gate adds `Edit` and `Write` to the allowlist and removes the denylist entirely, relying on explicit file paths in the turn's assignment to bound the scope.

This is live and verified against claude 2.1.289.
