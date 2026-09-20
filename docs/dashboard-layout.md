# Dashboard layout: a short, wide live frame

Design only; no implementation. Read alongside the approved
[metrics design](dashboard-design.md). Its metric definitions remain authoritative.
The user's later constraint overrides its dismissed-row proposal: dismissed crew
never appear individually. Their session tokens and cost remain in TOTAL forever.
Here, **retired means dismissed**, not idle, waiting, or merely finished working.

## Recommendation

Keep the approved seven-column table and put one plain-text annotation immediately
below TOTAL: **TOTAL includes retired(N): … tok / $…**. It is an explanation of the
total, not another amount to add. Keep both lines visible on every refresh, including
when the roster overflows. Use no borders, blank spacer rows, colour, or dim text.

This fits three people plus the column header, TOTAL, and its explanation into six
rows. A dismissed person consumes no roster space regardless of how many retire.
At 12 rows, captain plus eight current crew all fit. The footer replaces the
metrics proposal's separate explanatory header rather than adding more chrome.

## Research: observations and what to borrow

Sources consulted 2026-09-20. These are primary project documentation/source and
documented interaction patterns, not a usability study. The layout decisions in
the final column are design inferences for this particular pane.

| Source | Observed pattern | Application here |
| --- | --- | --- |
| [htop changelog](https://github.com/htop-dev/htop/blob/main/ChangeLog) | Supports hiding meters and the function bar, and explicit NO_COLOR support. | Large overview meters and help chrome are optional costs; spend scarce rows on people and accounting. |
| [k9s configuration](https://k9scli.io/topics/config/) | Separate switches hide the header, logo, and breadcrumbs. | Remove decorative/context chrome before data. No permanent title block. |
| [k9s commands](https://k9scli.io/topics/commands/) | Filtering and describe/view/log actions separate the resource list from detail. | Keep one row per current person; do not embed historical per-person detail. This pane has no interactive expansion. |
| [btop README](https://github.com/aristocratos/btop) | Process filtering, selectable graph symbols, selected-process detail, low-colour/TTY modes; documents font problems with graph characters. | Borrow the compact visual meter, not multi-panel graphs. ASCII is the reliable baseline. |
| [lazygit configuration](https://github.com/jesseduffield/lazygit/blob/master/docs/Config.md) | Normal/half/full focused views; automatic portrait layout considers both width and height (documented defaults: width at most 84, height at least 46). | Width alone must not trigger vertical stacking in a six-row pane. Keep a table and shorten horizontally. |
| [ccusage daily reports](https://ccusage.com/guide/daily-reports) | Wide mode at 100 columns; compact mode below it. Model breakdown is optional under the aggregate. | Use explicit width degradation and keep aggregates meaningful without all detail; avoid its tall boxed report style here. |
| [Codex status surfaces source](https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget/status_surfaces.rs) | Separate context-used/context-left percentage labels, compact token counts, and cumulative used-token items. | State used context consistently; keep CTX NOW and cumulative CUM TOK in distinct columns. Source observations, not a claim that an issue proposal shipped. |
| [Claude Code status lines](https://code.claude.com/docs/en/statusline) | Examples pair a context bar with a percentage, show cost with two decimals, and allow multiple lines and ANSI colour. | Pair an approximate bar with a readable number. Multiline capability does not justify spending two rows per person. |
| [GNU du manual](https://www.gnu.org/s/coreutils/manual/html_node/du-invocation.html) | Summary/depth controls hide detail. Its threshold filter explicitly does not remove those entries from a grand total. | Visibility and accounting are separate. Unlike a bare filtered total, explicitly disclose the hidden contribution. |

The shared useful pattern is a stable summary plus selectively visible detail.
Interactive tools can expose detail on demand; this two-second text refresh loop
cannot. Therefore the hidden amount must be printed, not explained by a tooltip,
keyboard shortcut, colour legend, or a promise of a future detail screen.

## Three rendered alternatives

All three use the same illustrative data, not newly researched prices. Captain:
2,700,000 tokens / $2.66; Jack: 130,000 / $0.07; Will: 366,000 / $0.09;
one retired crew: 3,104,000 / $0.56. Exact total: 6,300,000 / $3.38.
Current rates are $1.84 + $0.05 + $0.06 = $1.95/h. Retired contributes no rate.

The user's two-person excerpt cannot produce $1.95/h from $1.84 and $0.05 alone.
Will supplies the missing $0.06/h in these examples. With just captain and Jack
current, the displayed rate total would be $1.89/h; retired spend cannot explain
that difference. Rounded token labels may differ slightly when added by eye.

### A. Retired spend in a header

```text
TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded
NAME    AGENT                STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5        idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra  idle     #....  11%    130k  $0.07   $0.05
Will    claude/haiku-4-5     idle     ##...  31%    366k  $0.09   $0.06
TOTAL   -                    -        -            6.30M  $3.38   $1.95
```

Six rows, no historical roster. Readers learn the scope before reading the table.
But the explanatory values sit furthest from the total they explain, especially
with eight crew. A growing title/header also competes with this essential line.

### B. One aggregate retired row

```text
NAME    AGENT                STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5        idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra  idle     #....  11%    130k  $0.07   $0.05
Will    claude/haiku-4-5     idle     ##...  31%    366k  $0.09   $0.06
RETIRED 1 dismissed          -        -            3.10M  $0.56       -
TOTAL   -                    -        -            6.30M  $3.38   $1.95
USD list-price estimates; rounded; TOTAL is session-cumulative
```

The arithmetic is easiest to scan because retired amounts align with the numeric
columns. One aggregate respects the prohibition on individual dismissed rows.
However, this requires seven rows with the estimate label, presents history as a
roster member, and forces overflow sooner. It is reasonable for a ledger, less
useful for this short live pane.

### C. TOTAL annotation (recommended)

```text
NAME    AGENT                STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5        idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra  idle     #....  11%    130k  $0.07   $0.05
Will    claude/haiku-4-5     idle     ##...  31%    366k  $0.09   $0.06
TOTAL   -                    -        -            6.30M  $3.38   $1.95
TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded
```

Six rows, with the explanation beside its referent. Repeating TOTAL makes the
relationship explicit; “includes” prevents double counting. A side annotation
on the TOTAL line would save a row at 120 columns but overflow at 80. Keep one
consistent footer location instead of moving this explanation as the pane resizes.

## Accounting presentation contract

- The identity is: TOTAL tokens/cost = current people + retired people. If current
  people are folded for height, include that aggregate exactly once too.
- On dismissal, remove the person's row and transfer its cumulative token/cost
  contribution into the retired annotation in the same frame. TOTAL tokens and
  cost never decrease. This is a display invariant, not a new accounting design.
- TOTAL rate remains the approved current-roster rate and can fall. The
  never-shrink rule applies to cumulative tokens/cost, not CTX NOW or $/h 10m.
  Do not put a retired rate in the annotation; it would imply historical spend is
  still burning.
- With no retired crew, retain the footer as `TOTAL is session-cumulative; USD list
  est; rounded`. With retired crew that spent zero, still show their count and zero
  amounts. The footer never disappears merely because values are zero.
- Dismissal, `/clear`, and missing transcripts must not reset cumulative amounts.
  A missing update is not a negative update. Data retention is implementation work
  governed by the metrics design; this document does not claim the renderer alone
  can guarantee it.
- Unknown remains unknown: `-` for unavailable usage, `$?` for unavailable pricing,
  never a fabricated zero. If an aggregate contains unknown contributions, mark a
  known subtotal with `+?` (for example `$3.38+?`) and replace `rounded` with
  `+? incomplete`. Retain known cumulative amounts while exposing incompleteness.
  Do not present the known subtotal as the entire total.

## Columns, meter, and numbers

Keep `NAME AGENT STATUS CTX NOW CUM TOK CUM $ $/h 10m` in exactly that order.
Identity and state anchor the left; the three quantities form a numeric block on
the right.
Keep captain first, then crew in creation order. Do not sort by a changing rate
every two seconds: movement would make comparison harder. Keep native one-word
names separate from model and assignment; no task descriptions in this table.

Every header carries its unit, because the number alone does not say it: CUM TOK
and CUM $ accumulate over the whole session, CTX NOW is only the last turn, and
`$/h 10m` is a rate over the trailing ten minutes. Without the scope on the label
a cumulative count sits beside a percentage that resets and a window rate, and all
three read as the same kind of number. `10m` is the rendered form of the
implementation's window constant; if that window changes, so does the label.

Left-align text and its headers. Right-align CUM TOK, CUM $, $/h 10m and their
headers; right-align the percentage within CTX NOW. Use spaces, not tabs or
vertical rules. Suggested base widths are NAME 7, AGENT 20, STATUS 8, CTX NOW 10,
CUM TOK 7, CUM $ 6, $/h 10m 7, plus six one-space gaps: 71 character cells. The
two seven-cell numeric columns are sized by their headers, not their values, which
is why they exceed the six cells the money format needs. Grow NAME/STATUS
to fit actual values before consuming AGENT slack. Reserve the last terminal
column to avoid autowrap. Sizes are terminal cells, not encoded byte lengths.
Keep widths stable within a width tier; never let one tick's shorter number move
the neighbouring columns. Extra width restores full model identifiers first.

The bar is five ASCII cells followed by a space and a four-cell percentage:
`.....   7%`, `#....  11%`, `##...  31%`, `####.  80%`, `##### 100%`.
Use nearest fifth, half-up: filled cells = floor(percent / 20 + 0.5), clamped
to 0..5. Thus 7% has zero filled cells and 11% has one; the number supplies the
precision. Never inflate tiny usage to a visibly half-full bar. Render integer
used percent, not percent left. Unknown CTX NOW is `-`, with no empty bar
implying 0%.
No animation between updates, brackets, Unicode fractional blocks, or sparkline.

Use decimal token suffixes k/M/B, three significant digits after scaling, and
whole tokens below 1,000: `999`, `1.00k`, `130k`, `2.70M`, `6.30M`. Promote to the
next suffix when rounding would produce 1000 of the smaller unit. This buys more
reconciliation precision than `2.7M` without long integers. Compute totals before
formatting, never by summing labels. The footer's `rounded` explains the remaining
small differences; do not invent a rounding-adjustment spend row.

Money uses USD `$` plus two decimals, including zero: `$0.00`, `$2.66`, `$18.40`.
Both cost and rate use the same precision; CUM $ is cumulative, $/h 10m is the
approved trailing-window rate. An idle crew renders `$0.00` there and that is
correct: the window is empty, so nothing is burning. Do not render an idle rate as
`-`; `-` is reserved for unknown, and the difference between "not burning" and
"cannot tell" is the one the reader needs. A positive amount below half a cent is
`<$0.01` rather than an apparent zero. Allow numeric columns to grow for large
values, recovering width as described below; never clip the dollar digits.
`USD list est` applies to both money columns and means list-price estimate, not a
subscription invoice.

## Height: eight crew is nine people

The mandatory overhead is three rows: column labels, TOTAL, and its annotation.
At height H, H-3 individual rows fit without overflow. Optional session id,
elapsed time, and update clock may occupy one top line only if doing so hides no
person; remove that metadata line first when space is needed. No blank rows.

| Pane height | Eight current crew plus captain |
| --- | --- |
| 12 rows | All nine people, labels, TOTAL, annotation. No metadata line. |
| 10 rows | Six people (captain + five crew), one MORE(3) row, labels and footer. |
| 8 rows | Four people (captain + three crew), one MORE(5) row, labels and footer. |
| 6 rows | Two people (captain + one crew), one MORE(7) row, labels and footer. |

When overflowing, reserve one of the individual-row slots for `MORE(N)`: **current
crew hidden by pane height**, never retired crew. Its AGENT cell says `current;
resize`, CTX NOW and STATUS are `-`, and CUM TOK/CUM $/$/h 10m sum only those
hidden current crew. These are the same approved metrics aggregated for
presentation. Do not average context or pretend all hidden crew share a status.
Widening the pane does not reveal more rows; make it taller. Do not automatically
rotate pages every two seconds or imply that this plain refresh loop supports
interactive scrolling.

Six-row overflow example, using a separate illustrative session:

```text
NAME    AGENT                STATUS   CTX NOW    CUM TOK  CUM $ $/h 10m
CAPTAIN claude/opus-5        idle     .....   7%   2.70M  $2.66   $1.84
Jack    codex/gpt-5.6-terra  idle     #....  11%    130k  $0.07   $0.05
MORE(7) current; resize      -        -            1.47M  $0.65   $0.60
TOTAL   -                    -        -            7.40M  $3.94   $2.49
TOTAL includes retired(1): 3.10M tok/$0.56; USD list est; rounded
```

Here 2.70M + 0.13M + 1.47M + 3.10M = 7.40M, and $2.66 + $0.07 + $0.65 +
$0.56 = $3.94. Rates sum to $2.49 without retired. This is honest accounting,
but hidden crew's individual status/context cannot be monitored at six rows.
That limitation is real: enlarge to 12 rows to monitor all eight crew at once.
Stable creation order is preferred to an unrequested alert-ranking mechanism.

## Width degradation and plain-text styling

At 80-120 columns, keep all seven columns and the bar. The examples leave space
for longer names/statuses and numeric growth. Apply these steps in order only
when measured content no longer fits, keeping one column unused for autowrap:

1. Remove optional metadata; use one-space column gaps. Strip the approved model
   date suffix. Do not stack cells into multiple lines.
2. Shorten AGENT using existing model aliases with provider retained, such as
   `codex/terra`. If still necessary, truncate AGENT with a trailing `~`. Preserve
   the launcher name and readable status so people remain identifiable.
3. Drop only the five bar cells and their separating space; retain the CTX NOW
   percentage and shorten its header to `CTX`. This costs no metric. All numeric
   columns remain.
4. Below roughly 60 columns, omit AGENT before omitting any approved numeric
   metric. The exact breakpoint depends on actual name/status/money widths.
   Keep NAME, STATUS, CTX, CUM TOK, CUM $, $/h 10m. This is a compact view, not
   a change to what is measured.
5. Shorten the footer to `Incl retired(1): 3.10M tok/$0.56; USD est; rounded`.
   `USD est` still means list-price estimate here. If that cannot fit, reserve
   a second footer line and reduce roster capacity accordingly. Never truncate
   retired values or silently wrap over TOTAL.
6. If even those protected fields cannot fit (typically below 50 columns), replace
   the roster with a labelled resize notice and a compact TOTAL plus retired
   explanation. Do not output an apparently complete, clipped table. This is
   outside the requested 80-120-column target.

Use terminal default foreground/background throughout. No ANSI colour or dim in
the proposed frame: dim makes the indispensable retirement explanation harder to
read, and a retired spend value is not less financially real. Uppercase headers
and TOTAL, spacing, and adjacency provide hierarchy without colour dependencies.
No spinner, blinking warning, full-row inverse highlight, logo, or box border.
Refresh the same bounded frame every two seconds without accumulating frames in
scrollback; clearing/repainting mechanics are outside this design assignment.

## Review checks and limits

Check each rendered alternative at 80 and 120 columns: no line wraps, numeric
endpoints align, and retired values remain adjacent to an explicit inclusion
label. Check the recommended six-row frame and the six-row overflow frame;
calculate totals from their stated unrounded inputs. Mentally dismiss Jack:
his row disappears, retired increases, cumulative TOTAL stays fixed, rate can
fall. Never use a dismissed person's name in the footer.

These are text-layout and arithmetic checks, not a claim of live Herdr or
accessibility user testing. No code, provider integration, metric definition,
pricing implementation, new command, or interactive TUI is proposed by this file.
