# Crew dashboard: metrics design

Research and design only. No implementation. Covers what the token dashboard
(`src/captain_barbossa/dashboard.py`, fed by `src/captain_barbossa/usage.py`) should
show, why, and what to drop.

## 1. Who reads this and what they decide

One reader: the captain, watching three to six crew burn a shared budget in a Herdr
pane that refreshes every two seconds. Every column has to earn its width against a
decision that reader actually makes:

| Decision | Column that answers it |
| --- | --- |
| Is anyone stuck or waiting on me? | `STATUS` |
| Who is about to hit the context wall and needs splitting or a fresh session? | `CTX` |
| Who is costing the most, and is that crew worth it? | `COST` |
| Is money leaving faster than I meant it to, right now? | `$/h` |
| What has this whole session cost so far? | `TOTAL` |

Nothing else is decision-useful at two-second refresh. Anything that answers "why"
rather than "what now" belongs in a per-crew detail view, not the live frame.

## 2. The confirmed bug, and its real root cause

`dashboard.render()` skips dismissed crew before the row **and** before the totals:

```python
for crew in Crew.members(current):
    if crew.is_dismissed:
        continue  # dashboard.py:117-118 - skips the total too
```

So dismissing a crew makes the session total go down. The user observed
OUT 16k -> 12k, CACHE 3155k -> 3008k. Those tokens were spent and billed.

This is one instance of a general defect: **the total is computed from the current
live view rather than accumulated over the session.** There is at least one more
instance latent in the same code. `usage._transcript_path()` returns the *newest*
`transcript_path` in the events file:

```python
def _transcript_path(events_path):
    newest = None
    for record in _records(events_path):
        path = record.get("transcript_path")
        if isinstance(path, str) and path:
            newest = path  # earlier transcripts are dropped
    return newest
```

A crew that runs `/clear` gets a new session id and a new transcript file, and Claude
Code fires `SessionStart` with the new path. The moment that lands, the crew's totals
reset to near zero and the session total drops again. I checked 1,099 real event files
under the state root and found no file with more than one distinct `transcript_path`
(`SessionStart` sources seen: `startup` x21 only), so this has not fired in practice
yet - crew are short-lived. It is latent, not hypothetical: the check that proves it
is a `source` of `clear` or `resume` appearing in an events file.

Fixing only the dismissal path leaves the sibling broken. The rule below fixes both.

### Totals semantics, stated plainly

> **TOTAL is session-cumulative and only ever goes up.** It counts every crew this
> session has ever had, dismissed or not, and every transcript each crew has ever had.
> Dismissing a crew removes nothing from the total; it removes only the crew's ability
> to spend more.

One consequence to state on the frame, because it looks like an inconsistency:
`TOTAL COST` is not `TOTAL $/h` x elapsed. `COST` is cumulative over the whole session;
`$/h` is an instantaneous rate over a trailing window, and dismissed crew contribute
to the first and not the second.

This is buildable today. `dismiss_crew()` sets `record["status"] = "dismissed"` and
unlinks only `crew-<id>.sh`; the roster record and the events file both survive, and the
native CLI's transcript stays on disk. So a dismissed crew's tokens are still readable
and simply recomputing them each frame is correct - the transcript no longer grows.
The ceiling: if the native CLI prunes old transcripts (Claude Code's
`cleanupPeriodDays`, 30 days) the cost would silently fall back to `-`. Irrelevant
inside one live session; worth a comment if totals ever need to outlive one.

## 3. Evaluating the other four complaints

The brief asked me not to assume these were right. Three are, one is right about the
symptom and wrong about the fix.

**`IN 84` beside `CACHE 2686k` is not comparable - correct, and worse than stated.**
On Claude, uncached input is near zero by construction: a real assistant turn in this
repo's own transcript reads `input_tokens: 2` against `cache_read_input_tokens: 40597`.
`IN` is not merely incomparable, it is close to a constant zero for one of the two
providers. Meanwhile Codex reports cached tokens *inside* `input_tokens` and
`usage.py` already subtracts them, so `IN` means genuinely different things per
provider even after that correction. The column carries almost no information.

**`CACHE` merging read and write destroys cost - correct.** From the live LiteLLM
price map: `claude-opus-5` charges `5e-7`/token for a cache read and `6.25e-6` for a
5-minute cache write, `1e-5` for a 1-hour write. That is a 20x spread inside one
column. `CACHE 2686k` could be $1.34 or $26.86.

But splitting `CACHE` into two columns is the wrong fix. The merge only hurts because
`CACHE` is being asked to stand in for cost. Once there is a real `COST` column
computed from the unmerged fields, the display merge stops mattering - and then
`CACHE` has no job left at all. **Add cost, then delete the cache column**, rather than
widening the frame with a second cache column. Cache-hit ratio is a tuning signal, not
a live-monitoring one; it belongs in a per-crew detail view.

**No cost figure - correct, and it is the single biggest gap.** `TOTAL` currently sums
tokens across models. 1M Opus cache-read tokens and 1M Haiku cache-read tokens are the
same number in that column and a 5x difference on the bill. Tokens are not summable
across models. Dollars are. A cross-model total in tokens is close to meaningless.

**Full-length model ids crowd the row - correct, cheap to fix.** `claude-haiku-4-5-20251001`
is 25 characters and the date suffix carries no decision. Strip a trailing `-YYYYMMDD`
(mechanical, lossless, no table to maintain) and fold `PROVIDER` and `MODEL` into one
`AGENT` cell: `claude/haiku-4-5`. `models.py` already holds short aliases
(`haiku`, `terra`, `opus`) via `model_names()` if the column still bites, but aliases
drop the version and the date strip does not, so prefer the strip.

## 4. What the data actually supports

Read from `usage.py`'s two sources, verified against real files on this machine.

### Claude (transcript JSONL, `message.usage`)

Present: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, plus - and `usage.py` does not currently read these -
`cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`,
`output_tokens_details.thinking_tokens`, `service_tier`, `inference_geo`, and a
per-record ISO `timestamp`.

The 5m/1h cache split matters: those two bill at 1.25x and 2x base input. Reading the
split makes cache-write cost exact rather than approximate, for one extra field.

Sub-agent turns (`isSidechain: true`) live in the same transcript and are already
summed by `usage_for_events`. That is correct for spend and should be stated, not
changed - Claude Code's own `/usage` attributes sub-agent usage to the parent too.

**Not present: any cost figure, and any plan-quota figure.** No `costUSD`, no
rate-limit record. I grepped; the hits were tool-result text.

### Codex (rollout JSONL, `payload.info`)

Present: `total_token_usage` (`input_tokens` inclusive of `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`,
`total_tokens`), `last_token_usage` for the current turn, `model_context_window`,
`turn_context.model`, and a per-record ISO `timestamp`.

Plus something Claude does not give us: a `rate_limits` object on every `token_count`
record, with `primary.used_percent`, `window_minutes`, `resets_at`, `plan_type`, and a
`credits.balance`. Real sample: `used_percent: 0.0, window_minutes: 10080,
plan_type: "prolite"`.

**Not present: cost.**

### The asymmetry, stated

| | Claude | Codex | pi |
| --- | --- | --- | --- |
| Token counts | yes | yes | **no** (no hooks, no transcript) |
| Cache read / write split | yes, plus 5m vs 1h TTL | read yes; write field exists, observed 0 | no |
| Context window | tabulated in `CONTEXT_WINDOWS` (flagged rot) | stated by the log | no |
| Cost | no | no | no |
| Plan quota % | **no** | **yes** | no |
| Reasoning/thinking tokens | yes | yes | no |

pi crew must render `-` in every usage cell, never `0`. They already do.

The quota asymmetry is why plan quota cannot be a column yet. See section 8.

## 5. Prior art

Twelve sources. What each one settled:

1. **[Claude Code "Manage costs effectively"](https://code.claude.com/docs/en/costs)** -
   `/usage` prints exactly the four-way split per model with a dollar figure:
   `claude-sonnet-4-6: 1.2k input, 5.3k output, 940.0k cache read, 50.0k cache write ($0.55)`.
   Critically: *"Claude Code computes the dollar figure locally from token counts at
   list price."* Local computation from tokens is the sanctioned approach, not a hack.
   The page also warns that on a subscription *"the session cost figure isn't relevant
   for billing purposes"* - the label matters. And on agent teams, the direct analogue
   of crew: *"token usage is roughly proportional to team size... shut down teammates
   when their work is done."* Watching per-crew spend is the intended workflow.
2. **[Claude Code status line reference](https://code.claude.com/docs/en/statusline)** -
   the field list a script receives: `cost.total_cost_usd`, `cost.total_duration_ms`,
   `context_window.used_percentage`, `rate_limits.five_hour.used_percentage`,
   `rate_limits.seven_day.used_percentage`, `prompt_cache`. Also the definition we are
   currently getting wrong: *"`used_percentage` is calculated from input tokens only:
   `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. It does not
   include `output_tokens`."*
3. **[ccusage](https://github.com/ccusage/ccusage)** and
   **[its report columns](https://ccusage.com/guide/daily-reports)** - the de facto
   standard layout: `Date | Models | Input | Output | Cache Create | Cache Read |
   Total Tokens | Cost (USD)`. Cache create and cache read are always separate, never
   merged. It adapts to terminal width, dropping to essential columns under 100 chars.
4. **[LiteLLM `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)**
   and the **[custom cost map docs](https://docs.litellm.ai/docs/proxy/custom_model_cost_map)** -
   the community's answer to price rot. ccusage sources pricing from it, fetching at
   runtime with a build-time fallback.
5. **[Codex issue #3630, "Confusing statusline in terms of tokens used vs % context left"](https://github.com/openai/codex/issues/3630)** -
   users cannot reconcile a raw cumulative token count sitting next to a percentage
   that resets. Mixing the two units in one region confuses people. Our current
   `62k/200k 31%` cell does exactly this.
6. **[Codex issue #27984, "Render context and usage status-line percentages as compact bars"](https://github.com/openai/codex/issues/27984)**
   and **[#21324](https://github.com/openai/codex/issues/21324)** - Codex converged on
   `CTX ██░░░ 31%`: a bar plus a percent, no raw pair.
7. **[NTM (Named Tmux Manager)](https://github.com/Dicklesworthstone/ntm)** - the
   closest structural analogue: tiled panes of Claude/Codex/Gemini agents. Its
   dashboard shows **token velocity badges** per agent, explicitly to spot *"an agent
   that has gone into a loop versus one making steady progress."* Rate, not just total.
8. **[Conductor](https://www.conductor.build/)** - parallel Claude Code agents on a
   Mac. Shows status and diffs per agent but no spend column; reviewers note only that
   *"running agents in parallel burns quota fast."* The gap this design fills is real
   and not yet filled by the nearest commercial product.
9. **[Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)** -
   burn rate in tokens/min or "% of budget per hour", plus time-to-empty at current
   rate. Designed to be kept open in a side pane, which is exactly our form factor.
10. **[BurnMeter](https://github.com/Viben69/BurnMeter)** - the honest framing for
    subscription users: shows "retail" (what the usage would cost at API rates) beside
    "the fee" (the flat subscription), never pretending the first is a bill. Its live
    gauge shows burn rate, tokens/min, 5-hour and weekly limit percentages, context
    window use.
11. **[anthropics/claude-code issue #94289, "Token burn-rate 'speedometer' per session and overall"](https://github.com/anthropics/claude-code/issues/94289)** -
    our exact problem, requested upstream: *"When multiple sessions run in parallel,
    users cannot identify which session is consuming tokens excessively until the
    budget is depleted."* Proposes tokens/min or % of budget per hour, a trailing
    10-minute smoothing window, colour bands, and explicitly that a parent's rate must
    include its sub-agents.
12. **[Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)** -
    cache write is 1.25x base input at the 5-minute TTL and 2x at 1 hour; cache read is
    0.1x. Confirmed against the live LiteLLM entries for `claude-opus-5`
    (`5e-6` / `6.25e-6` / `1e-5` / `5e-7`).

The convergence across all twelve: **a percentage (not a raw pair) for context, a
separated or costed cache, a rate alongside the total, and a dollar figure that is
labelled as an estimate.**

## 6. Proposed frame

```
session 618fa8df  4 crew (1 dismissed)  elapsed 1h12m  13:58:02
totals are session-cumulative at list price; dismissed crew included

NAME     AGENT               STATUS     CTX         TOKENS  COST   $/h
CAPTAIN  claude/opus-5       idle       ▇▇░░░  7%     2.7M  $2.66  $1.84
Jack     codex/gpt-5.6-terra idle       ▇░░░░ 11%     130k  $0.07  $0.05
Will     claude/haiku-4-5    idle       ▇▇░░░ 31%     366k  $0.09  $0.06
Gibbs    claude/haiku-4-5    dismissed  -             3.1M  $0.56  $0.00
TOTAL    -                   -          -             6.3M  $3.38  $1.95
```

72 columns. The current frame is 75 and carries less.

Token figures are the user's live frame; costs are computed from it at current list
prices, assuming a plausible read/write split of the merged `CACHE` cell (the frame
cannot distinguish them - which is the point).

### Column definitions

| Column | Definition |
| --- | --- |
| `NAME` | unchanged |
| `AGENT` | `provider/model`, model with a trailing `-YYYYMMDD` stripped. Replaces `PROVIDER` + `MODEL`, saves ~9 chars |
| `STATUS` | unchanged, plus `dismissed` as a real value instead of a hidden row |
| `CTX` | percent of the context window used by the last turn, input-only (see 7.8), with a 5-cell bar. `-` for a dismissed crew: the process is gone and its last context answers nothing |
| `TOKENS` | session-cumulative billable tokens, all four kinds summed. The neutral "how much did this crew consume" figure, and the only one meaningful on a subscription where dollars are counterfactual |
| `COST` | session-cumulative USD at list price, computed per model from the four unmerged token kinds |
| `$/h` | cost rate over a trailing 10-minute window of assistant turns. `$0.00` for an idle or dismissed crew is correct and useful - it says they are not burning. `-` when no timestamped turn exists |

### What the header carries

Session id, crew count with the dismissed count in parentheses, elapsed wall time, the
clock. Then one fixed line stating totals semantics and the list-price caveat. That
line is two seconds of reading once, and it removes the entire class of "why did the
number go down" and "is this my actual bill" confusion. Worth its two rows.

## 7. Ranked changes

Ordered by decision value per line of diff.

**1. Totals never shrink.** Drop the `is_dismissed` skip from the totals path, and
accumulate across every transcript an events file has ever named, not just the newest.
Fixes the reported bug and the latent `/clear` sibling in one place. The lazy version
of the first half is literally deleting two lines: stop skipping the row entirely and
let dismissed crew render with `STATUS dismissed` and `$0.00/h`. That is a smaller
diff than filtering rows and unfiltering totals, and it answers "where did the money
go" directly. If long sessions get noisy, collapse to a count in the header later.

**2. Add `COST`.** The only column comparable across models, and the audience is
watching spend. Without it, `TOTAL` sums Opus tokens and Haiku tokens as equals.

**3. Delete `IN`, `OUT` and `CACHE`; add one `TOKENS`.** `IN` is near-constant zero on
Claude and means something different on Codex. `CACHE` is only load-bearing because it
was standing in for cost, and (2) retires that job. Three columns out, one in: the row
gets shorter and more informative at the same time.

**4. Fold `PROVIDER` + `MODEL` into `AGENT`, strip the date suffix.** Nine characters,
a one-line regex, no table.

**5. `CTX` as a percent with a bar, drop the raw `62k/200k` pair.** Every comparable
tool converged here, and Codex #3630 is the record of what mixing the units costs.

**6. Add `$/h`.** The metric NTM, Claude-Code-Usage-Monitor, BurnMeter and
claude-code#94289 all independently arrived at. It is the only column that catches a
looping crew *before* the budget is gone rather than after. Both providers timestamp
every record, so a trailing 10-minute window is computable today.

**7. Source prices from LiteLLM, cached.** See section 8. Also retires half of
`usage.CONTEXT_WINDOWS`, which the file itself flags as *"the known rot."*

**8. Make `CTX` input-only.** `_context_cell` uses `sum(counts.values())`, which
includes `output_tokens`. Claude Code's own `used_percentage` is
`input + cache_creation + cache_read`, output excluded. Ours therefore reads a little
higher than the crew's own `/context`, and two numbers for one fact that disagree is
worse than either alone. One-line fix, low impact, but free.

**9. (Later) plan quota.** Section 8.

## 8. Cost, and pricing without a rotting table

### Show cost - yes, with a label

The audience is professionals watching spend, and dollars are the only unit that is
summable across models. But the figure is an estimate, not a bill, and on a
subscription it is counterfactual. Claude Code itself states this about `/usage`; the
header line in section 6 carries the same caveat in five words. BurnMeter's framing
(retail versus fee) is the honest maximal version; we do not need it, because the crew
dashboard's question is comparative ("which crew is expensive") and a consistent
list-price number answers that regardless of what is actually billed.

### Sourcing

Do not hand-maintain a price table in source. `usage.CONTEXT_WINDOWS` is already one
such table and its own comment calls it the known rot; a price table would be worse,
because prices change without a new model shipping.

Use **LiteLLM's `model_prices_and_context_window.json`**. Verified against the live
file today: every model in `models.MODELS` is present under its exact id -
`claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`, `claude-fable-5-1`,
`gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-5.5`, `gpt-6-astra` - each with
`input_cost_per_token`, `output_cost_per_token`, `cache_read_input_token_cost`,
`cache_creation_input_token_cost`, `cache_creation_input_token_cost_above_1hr` and
`max_input_tokens`. No name mapping needed; `usage.context_limit()`'s
longest-prefix-match already handles dated ids.

Shape of the mechanism:

- Fetch once, keep **only** the models this project can launch, discard the rest. The
  file is 2.8 MB and 4,320 entries; caching it whole would be absurd.
- Cache the extract in the project state dir with a TTL (a day is generous - prices
  move on the order of months).
- Offline or fetch failure: use the cached extract. No cache and no network: render
  `$?`, never `$0`. A confident zero is worse than an admitted unknown.
- `CAPTAIN_PRICES` pointing at a JSON file overrides, for contracted rates. This
  mirrors Claude Code's own `modelPricing` managed setting, which exists for exactly
  this reason.

`max_input_tokens` in the same file retires the Claude half of `CONTEXT_WINDOWS`
(it reports 200,000 for Haiku 4.5 and 1,000,000 for Sonnet 5 / Opus 5 / Fable 5.1,
matching the hand-kept table exactly). **Keep Codex reading its own log**: LiteLLM
reports `max_input_tokens: 922000` for `gpt-5.6-terra` while the rollout reports
`model_context_window: 258400`. The API window and the CLI's effective window are
different numbers and the native log is authoritative about the one the crew actually
has.

### Cost accuracy ceilings, to be honest about

- **Cache TTL**: reading `cache_creation.ephemeral_5m_input_tokens` and
  `ephemeral_1h_input_tokens` separately makes Claude cache-write cost exact
  (1.25x vs 2x). `usage.py` currently reads only their sum, which prices 1-hour writes
  as 5-minute ones and undercounts. One extra field.
- **Codex long context**: above 272k tokens per request, Codex input and output prices
  double (`input_cost_per_token_above_272k_tokens`). The rollout gives per-turn totals
  so this is derivable in principle, but it is per-request accounting for a rare case.
  Skip it; note that very long Codex turns are undercounted.
- **Claude service tier and data residency**: the transcript carries `service_tier` and
  `inference_geo`, and residency bills at 1.1x. Ignore both; note it.
- **Subscription reality**: none of this is what anyone is charged on Pro/Max.

## 9. What we do not have

**Claude plan quota (5-hour and 7-day used percent).** Codex writes `rate_limits` into
every `token_count` record. Claude writes nothing equivalent to the transcript. So a
`QUOTA` column would today be populated for Codex crew and blank for Claude crew, and
a column that is blank for the majority provider is worse than no column. Left out of
the proposed frame deliberately.

There is a path, and it is worth recording because it also solves cost. Claude Code
passes `rate_limits.five_hour.used_percentage`, `rate_limits.seven_day.used_percentage`
and `cost.total_cost_usd` to a **status line command**, and `instructions.native_args()`
already builds a `--settings` JSON dict for every Claude crew - adding a `statusLine`
key beside `hooks` is one line. The crew's own Claude Code would then write
authoritative, Anthropic-computed cost and real quota percentages into the events file
we already tail. That would make the `COST` column exact for Claude crew and
retire the price map for them entirely.

The cost of that path: it replaces whatever status line the user has configured, inside
the crew's pane. That is a visible change to someone else's tool, so it is a follow-up
with its own decision, not part of this change. And it would not help Codex, which
still needs the price map - so the price map is required either way, and doing it
first is correct.

**Not available at all, by any route:**

- Actual billed amounts. Local computation only ever estimates.
- Any usage for pi crew: no hooks, no notify, no transcript. `-` forever, not `0`.
- Per-crew wall-clock "time spent working" distinct from elapsed: derivable roughly
  from turn timestamps, but nothing records when a crew was blocked on a prompt versus
  thinking. Not proposed.
- Cache-hit ratio per crew: derivable (`cache_read / (cache_read + cache_creation)`),
  but it is a tuning signal, not a live-monitoring one. Belongs in a per-crew detail
  view if one is ever wanted, not in the frame.

## 10. What to drop

- `IN`, `OUT`, `CACHE` - three columns, replaced by `TOKENS` + `COST` (change 3).
- `PROVIDER` as its own column - folded into `AGENT` (change 4).
- The raw `62k/200k` pair in `CONTEXT` - percent and bar carry the decision (change 5).
- The model id's date suffix (change 4).
- The `is_dismissed` skip (change 1).

Net: the row loses four columns and gains three, gets narrower, and answers two
questions it previously could not - what this costs, and how fast.
