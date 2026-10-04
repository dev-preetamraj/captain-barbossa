# pi headless turn

A quiet turn with pi runs one paneless invocation in headless mode.

## Headless argv

```
pi -p --mode json --no-session --no-context-files --tools read [--model MODEL] TASK
```

Write gate adds `edit` and `write` to `--tools`:

```
pi -p --mode json --no-session --no-context-files --tools read,write,edit [--model MODEL] TASK
```

## Result reader

A headless pi turn writes JSONL. The reader looks for the last event with `"type": "turn_end"`,
checks that `stopReason` is either `"stop"` or `"end_turn"` (both mean the turn finished; pi
relays its provider's own word), rejects `"error"` and `"toolUse"` and truncations, and joins
the text from all blocks in the `message.content` array.

A successful turn on xai ends with `stopReason: "stop"`. Grok ends with `end_turn`. Both are
valid; pi accepts either.

## Spend reported

`usage.cost.total` for USD, `usage.totalTokens` for token count. Either may be missing and that
is not an error.

## Verification notes

pi exited 0 with `stopReason: "error"` and empty content on some test runs across different
providers. So every reader decides from the result document, never from exit status.

pi does not say `end_turn` - the reader was written against Grok's own word first and would
have rejected every successful pi turn until the `stopReason` check accepted both forms. The
argv reads a file outside the cwd and `--tools read` blocks the write with no file created.

Verified live against pi 0.87.1: the write was blocked, and the success read worked, but
only on the fourth attempt. The earlier three failed on provider transport errors, not on
pi itself.
