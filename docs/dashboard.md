# Dashboard

`captain dashboard` refreshes a plain-text table of the session's captain and
crew in the current pane. It refreshes every 2 seconds by default; use
`--interval SECONDS` to change that.

```sh
captain dashboard
captain dashboard --interval 5
captain dashboard --refresh-prices
```

A captain can also open it for you: set `[dashboard] enabled = true` in
`.captain/settings.toml` and launching `captain` splits a second pane below
itself, titled **Dashboard**, running the same table. It is off by default;
`captain --no-dashboard` skips it for one launch even when it is enabled. See
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md)
for every dashboard setting.

```text
NAME    AGENT               STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5       idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra idle     #....  11%    130k  $0.07   $0.05
Will    claude/haiku-4-5    idle     ##...  31%    366k  $0.09   $0.00
TOTAL   -                   -        -            6.30M  $3.38   $1.89
TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded
```

## Rows and columns

A `CAPTAIN` row leads the table, then one row per current crew in recruit
order. The captain writes the same lifecycle hook events its crew do, so it
carries usage too; its live status is matched by pane, since the captain has
no agent name of its own. Every frame re-reads the roster, so crew recruited
while it runs appear in the next frame, and any row the dashboard cannot read
degrades to `-` instead of breaking the frame.

- `AGENT` is `provider/model`, with a trailing `-YYYYMMDD` and the provider's
  own prefix dropped (`claude-opus-5` reads `claude/opus-5`). It follows a
  model switched mid-session, not the one recruited with.
- `STATUS` comes from Herdr in one call per frame, like `captain status`.
- `CTX NOW` is the context the last API call actually carried, as a percent of
  the model's window, with a five-cell bar rounded to the nearest fifth. Small
  usage stays visibly small: `.....   7%` is 7%, not an empty reading. It is a
  live reading and falls back to near nothing after `/clear`.
- `CUM TOK` is session-cumulative, all four token kinds summed over every API
  call so far, in `k`/`M`/`B` at three significant digits. It survives `/clear`:
  a crew that starts a new transcript keeps the spend from its earlier one.
- `CUM $` is cumulative USD at list price.
- `$/h 10m` is a rate, not a total: the turns in the trailing 10 minutes,
  extrapolated to an hour. The header names the window because the number alone
  does not, and a rate that never said which minutes it covered would be
  unreadable.

`$0.00` under `$/h 10m` is correct, not a broken column. It means that crew has
spent nothing in the last 10 minutes—it is not burning. That is exactly what
Will's row above shows: `$0.09` cumulative from work it already did, and a zero
rate because it has been quiet longer than the window. `CUM $` never falls; the
rate drops to zero as soon as the window empties, and climbs again on the next
turn.

A zero is always a measured zero. The dashboard never fabricates one: `-` means
the value is unknown—no usage it could read, or turns that carry no timestamp—
and `$?` means the price could not be resolved. So `$0.00` says "nothing", `-`
says "cannot tell", and the two are never interchanged.

## Cumulative usage and live context

`CUM` and `NOW` are different units and do not compare. A single reply to you is
many API calls—one per tool use—and every one of them re-sends the whole
conversation, so the same context is counted again on each call. A captain that
answered once with 21 tool calls on a 68k context had used 7% of a 1M window and
still billed 1.32M cumulative tokens, 96% of them cache reads of that one
context. `CUM TOK` far exceeding `CTX NOW` is the normal case, not a fault:
`CUM TOK` only ever grows, `CTX NOW` rises and falls with the conversation.

Dismissed crew get no row of their own. Their tokens and cost stay inside
`TOTAL` and are disclosed by the annotation under it, which never disappears—
with nobody retired it reads `TOTAL is session-cumulative; USD list est;
rounded`. So the cumulative `TOTAL` columns never shrink when someone is
dismissed, while `TOTAL $/h 10m` counts only the current roster and falls:
retired crew are not burning anything. A total built from partly unknown parts
keeps the known subtotal and marks it: it renders `$3.38+?` and ends its footer
with `+? incomplete`, rather than passing the subtotal off as the whole.

`CUM $` is an estimate at published list prices, not a bill. On a Claude or
ChatGPT subscription it is counterfactual: it says what these tokens would
have cost on the API, which is the only comparable number across providers.

## Prices and context windows

Prices come from LiteLLM's `model_prices_and_context_window.json`, cached at
`~/.local/state/captain-barbossa/prices.json` (or the configured state root).
Passive usage reads use this cache without network calls, directory creation,
or cache writes, even when stale. `captain dashboard --refresh-prices` explicitly
opts into one background refresh attempt when the cache is missing or older than
a day; frames keep using existing data while it runs. Without cached prices,
money columns read `$?` rather than a confident `$0.00`.
Point `[dashboard] prices_file` at a JSON file of the same shape to override
it, for a negotiated rate or a model LiteLLM does not carry.

Context windows prefer the same LiteLLM data and fall back to a small bundled
table of the Claude models Captain launches, so `CTX NOW` still works on a cold
cache or offline. `[dashboard] context_limit` beats both, for a model neither
knows or to measure against a smaller ceiling than the model's own:

```toml
[dashboard]
prices_file = "~/prices.json"
context_limit = 225000
```

Codex crew are read from their own rollout log, which states tokens, the model
actually in use, and the context window the CLI enforces, so they carry the
same columns Claude crew do. pi installs no hooks and writes nothing we can
read, so a pi crew shows its name, agent and status with `-` for usage.

## Pane layout

The frame fits the pane rather than wrapping. Short panes keep the column
labels, `TOTAL` and its annotation, and fold the crew that do not fit into one
`MORE(N)` row that sums exactly them; make the pane taller to see them
individually. Narrow panes drop the optional header line first, then shorten
model names, then the bar, then the `AGENT` column—the numbers go last.

When a crew pane splits into the captain's own tab, the dashboard is re-nested
directly under the captain; otherwise splitting the captain sideways leaves
the dashboard stretched under both panes. Herdr can only reparent a pane by
way of another tab, so the pane leaves and comes straight back and the tab
flickers once—that is deliberate. The dashboard pane is itself never chosen
as a split target for new crew.
