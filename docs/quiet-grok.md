# Grok quiet turns

A headless grok turn for bounded, single-answer work - a question, a review, a triage, a
focused edit - with no pane and no assignment.

## Headless mode

```
grok --trust -p TASK --output-format json --permission-mode dontAsk --deny Bash
```

The `-p` flag takes the task inline and `--output-format json` returns a structured
result. `--trust` and `--permission-mode dontAsk` skip approval dialogs. `--deny Bash`
is always present. The read-only gate adds `--deny Write --deny Edit` on top of it.

## Read-only gate

To block writes in a read-only turn:

```
--deny Bash --deny Write --deny Edit
```

With `--write`, the Write and Edit denies drop, keeping `--deny Bash`:

```
--deny Bash
```

## Result reading

Grok returns one JSON object. A successful turn has:

```json
{
  "stopReason": "end_turn",
  "text": "the answer",
  "num_turns": 1,
  "total_cost_usd": 0.0001
}
```

The reader checks `stopReason == "end_turn"` and reads text from the `text` field.
Other `stopReason` values (`error`, `toolUse`, truncation) are failures.

The spend note prints from `num_turns` and `total_cost_usd`.

## Reads the session directory

Grok reads the whole filesystem; no flag needed to see the session directory. A
read-only turn can read mail, event logs and assignment records without restriction.

Verified live against grok 1.0.46: the write was blocked, and the success read worked.
