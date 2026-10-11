# Placement: declared tab shapes

Crew placement is declared, not inferred. You write the shape of a tab and the grid
fills it in a fixed order. There is no scoring, no measuring, and no negotiation: the
declared slot is created, however small it lands.

```toml
[placement]
captain_tab = [1, 2, 2]
crew_tab = [2, 2, 3]
```

Recruitment resolves all choices once under the crew metadata lock. Flags override
settings; split flags imply pane placement. With no placement setting, omitted
placement, direction and split target select the next automatic slot without a
picker. Agent comes from `[crew] agent` or `captain.json`; model comes from
`[crew] model`. The command prints these sources and rejects an invalid model
before any layout change. Explicit `--placement tab` rejects split flags.

## 1. Reading a shape

An array is one tab, read left to right as columns. Each number is how many panes that
column holds, stacked top to bottom.

`captain_tab = [1, 2, 2]` is three columns holding one, two and two panes:

```text
+-----------+-----------+-----------+
|           |  crew 1   |  crew 2   |
|  captain  |           |           |
|           +-----------+-----------+
|           |  crew 3   |  crew 4   |
+-----------+           |           |
| dashboard |           |           |
+-----------+-----------+-----------+
```

- `captain_tab` shapes the tab the captain launched in. Column 1 is the captain alone,
  so its first number must be 1.
- `crew_tab` shapes every other tab.
- Crew a tab holds is the sum of its columns, minus the captain: `[1,2,2]` holds four,
  `[2,2,3]` holds seven.
- The dashboard is not a slot. It nests under the captain inside column 1 and is
  invisible to the grid.

### Column 1 of the captain's tab is the captain alone

`captain_tab[0]` must be 1. The captain's pane is never split in two, so a shape like
`[2, 2, 2]` is refused by name (section 9), not clamped.

The reason is Herdr's: **it splits a pane, never a layout node.** `herdr pane split`
takes `--pane <ID>` with `--direction right|down` and has no whole-row form, and
`pane move --split right --target-pane <ID>` is pane-targeted the same way. While
column 1 is only the captain, the captain's pane *is* the row, so opening column 2 by
splitting it gives a full height column. Put one crew under the captain and no pane
spans the row any more: the next column opens beside part of column 1 instead of beside
all of it, and that crew still spans the full tab width beneath it. The declared shape
becomes unreachable rather than merely tight.

The dashboard is the one other pane in column 1, and it is not a slot: it nests inside
the captain's own share, so it costs the captain height and nothing else height or
position. That is why it stays while a stacked crew does not.

## 2. Schema

| Key | Type | Default | Means |
| --- | --- | --- | --- |
| `captain_tab` | list of ints >= 1, first 1 | `[1, 2]` | The captain's own tab. |
| `crew_tab` | list of ints >= 1 | `[2, 2]` | Shape of every other tab. |

The defaults reproduce the behaviour Captain shipped before geometric placement: two
crew beside a full height captain, four to a crew tab. `captain_tab = [1]` means
"never put crew in my tab", which is the other common want and costs one character.

`min_width` and `min_height` are deleted, and the `[layout]` table with them. Under an
absolute grid there is nothing for a minimum to do: a slot is never refused for being
small.

## 3. Fill order

Breadth first: a new column opens before anything stacks. Walk rows top to bottom, and
within a row walk columns left to right, skipping any column that has no such row and
skipping the captain's own slot.

**`captain_tab = [1, 2, 2]`, four crew:**

| Crew | Slot | Split |
| --- | --- | --- |
| 1 | column 2, row 1 | captain's pane, right |
| 2 | column 3, row 1 | crew 1's pane, right |
| 3 | column 2, row 2 | crew 1's pane, down |
| 4 | column 3, row 2 | crew 2's pane, down |
| 5 | tab is full | overflow, section 6 |

**`crew_tab = [2, 2, 3]`, seven crew:**

| Crew | Slot | Split |
| --- | --- | --- |
| 1 | column 1, row 1 | none: it takes the new tab's root pane |
| 2 | column 2, row 1 | crew 1's pane, right |
| 3 | column 3, row 1 | crew 2's pane, right |
| 4 | column 1, row 2 | crew 1's pane, down |
| 5 | column 2, row 2 | crew 2's pane, down |
| 6 | column 3, row 2 | crew 3's pane, down |
| 7 | column 3, row 3 | crew 6's pane, down |

## 4. Slot to split mapping

Two rules cover every slot:

- **Opening a column.** Split the **top pane of the nearest open column to its left**,
  to the right. For column 2 that is the captain, or crew 1 in a crew tab.
- **Stacking in a column.** Split that column's **bottom pane**, down.

A column's top pane is the pane of the earliest recruited crew still in it (the
captain, for column 1). Its bottom pane is the pane of the most recently recruited
crew still in it. Both hold under churn: a right split puts the new pane to the right,
a down split puts it below, so arrival order within a column is top to bottom, and
removing a pane does not reorder the survivors.

### Ratios, so a declared shape looks declared

Herdr halves a pane unless told otherwise. Halving alone makes `[1, 2, 2]` come out
1/2, 1/4, 1/4 rather than in thirds, so the new pane's own split is only part of the
job: a split resizes the two panes it touches and never a sibling already on screen, so
the rest of the chain has to be resized after it.

Evening happens in two places, and resolving a recruit is not one of them.
`Placement.resolve` touches nothing in Herdr, so a recruit that is then refused leaves
the tab as it was. It only records the chain to even in the `Spot`. `agents.create_crew`
splits at the default ratio, then calls `Placement.even_new_pane` with the new pane.

The chain is the whole column for a down split, and the row of every open column's top
pane for a right split, with the new pane inserted right after the one it split. It is
read from the roster, so a tab that filled, emptied and refilled is evened as it
actually stands: a crew reopening a column a dismissal emptied lands in the middle of
the row.

Herdr rounds every nested split to whole cells, so a ratio computed from the pane count
drifts by a cell or two (a 234 column tab with three panes landed 77/79/78). The
evening reads sizes instead (`Placement.even_chain`):

1. **Read the live layout** (`pane layout`) for the chain's total width or height, and
   split it with `layout.even_shares`: whole cells that differ by at most one, the
   remainder going to the first panes. 234 columns over three panes is 78/78/78.
2. **Walk the chain left to right** (top to bottom for a column). For each pane but the
   last, re-read the layout, and if it is short of or over its share, resize its
   trailing edge by that many cells as a fraction of the cells from it to the chain's
   end. The target is aimed a quarter cell past the share, so Herdr's floor or round
   both land on it.

The dashboard never appears in a chain: it is nested inside the captain's slot, so the
captain's own right edge is still the column boundary, and column 1 has no second slot
for it to sit above.

**Herdr clamps a split ratio to 0.1-0.9.** A chain of 11 or more panes has no reachable
even share, and a resize Herdr declines cannot be retried. Neither costs the crew: the
pane already exists, the declared slot is created as section 1 says it always is, and
the tab is left uneven with the reason on stderr.

```text
captain: tab not evened: Herdr cannot size 11 panes evenly in one tab: the smallest
share is outside its resizable range 0.10-0.90.
```

A direction forced by `--direction` carries no chain and so is not evened, because the
shares only describe the split the shape asked for. Herdr halves the pane as it would
have anyway.

## 5. Bookkeeping, from the roster

Choosing a slot reads the session roster and nothing else: it never measures a pane or
looks at what Herdr reports, and it moves nothing. That is what makes it deterministic:
the same roster and the same shape give the same answer, whatever order Herdr would have
listed panes in. Placement does call `pane layout`, but only afterwards, to even the
chain once the pane exists (section 4); the slot never depends on what it reads.

Each crew record gains its slot:

```json
{"pane": "w1:p7", "tab": "w1:t1", "column": 2, "row": 1}
```

To place a crew:

1. Pick the tab (section 6) and read its shape.
2. Count active crew per column in that tab.
3. Walk the fill order and take the first column whose count is below its capacity.
4. Derive the split from section 4, using the pane ids in the roster.

Occupancy is a **count per column**, not a set of row numbers. Rows are a way to read
the shape, not an identity a crew keeps.

### What a dismissal frees

A dismissed crew frees one place in its column, and Herdr collapses its split so the
surviving panes in that column grow into the space. The next crew stacks under that
column's new bottom pane.

Worked, on `captain_tab = [1, 2, 2]` holding crew 1 to 4:

- Dismiss crew 3, at column 2 row 2. Column 2 drops to one crew, and crew 1's pane
  grows back to the full column. The next crew takes column 2 again, splitting crew
  1's pane down: the same slot, rebuilt.
- Dismiss crew 1, at column 2 row 1. Column 2 still holds crew 3, whose pane grows to
  the full column. Column 2's top and bottom pane are both now crew 3's. The next crew
  splits crew 3's pane down, landing below it.
- Dismiss crew 1 and crew 3, emptying column 2. Herdr closes the column and the space
  goes to its sibling. Column 2 is no longer open, so the next crew opens it again by
  splitting the top pane of column 1 to the right, exactly as crew 1 did.

Nothing is renumbered and no crew record is rewritten when another is dismissed. Every
placement recomputes from counts, so there is no bookkeeping to drift.

#### Evening out after a dismissal

Herdr hands a closed pane's space to the panes *after* it in its chain, which land
exactly on their new share; only the panes before it are left short. A dismissal is
therefore a recruit run backwards (`placement.even_after_close`): the plan is read while
the crew still holds its slot, applied once Herdr has closed the pane, and grows each
pane before it by the step `re_even` would have shrunk it by.

The chain is the crew's own column when the column outlives it, and the row of column
tops when its pane was the column's last, since Herdr then hands the whole column to a
sibling. A crew placed by hand holds no slot, so nothing in the grid moved and nothing
is resized.

Geometry is cosmetic and the dismissal is not: a plan that cannot be computed, or cannot
be applied, is noted on stderr and the crew is dismissed anyway. A pane Herdr reports as
`pane_not_found` skips the evening entirely, because Herdr rebalanced the tab when that
pane went.

### What the captain's exit frees

The captain's own pane is the user's terminal, so it stays. What the session opened does
not: its live crew panes and its dashboard pane close when the captain's native CLI
exits, whichever way it exits, and each crew is recorded `dismissed` as its pane goes,
so resuming with `--session` finds no crew holding a slot no pane backs any more.

`captain` execs the native CLI over itself, so there is no exit hook to hang this on. It
forks a watcher first (`agents.close_panes_on_exit`), which polls `getppid()` once a
second and runs the teardown once it has been reparented away from the CLI. Polling
rather than waiting on an inherited pipe is deliberate: a pipe is held open by every
descendant the CLI forks, so one backgrounded job would mean no teardown at all. The
watcher takes its own session (`setsid`), so closing the captain's pane cannot take the
teardown with it, and it only ever closes pane ids this session's own records name. A
pane already gone is not news, and Herdr closes a tab whose last pane goes with it.

It is best effort at both ends: a watcher that cannot be forked is reported and the
captain launches regardless, and a Herdr call that fails during teardown does not stop
the remaining panes from closing.

### Editing the shape mid-session

The new shape applies to the next placement. Crew already placed keep their panes. A
shape edited to be smaller than what a tab already holds simply reads as full.

## 6. Tab overflow

In order, and the order is the roster's, not the screen's:

1. The captain's tab, if `captain_tab` has a free place.
2. Existing crew tabs, in recruitment order, first one with a free place.
3. A new tab, shaped by `crew_tab`, with the crew in column 1 row 1.

A new tab is created exactly as today, labelled with the crew's name, and its root pane
is the first slot rather than something to split.

## 7. Flags still win

Precedence is unchanged, stated once:

```text
CLI flag  >  <project>/.captain/settings.toml  >  ~/.captain/settings.toml  >  shipped defaults.toml
```

- `--placement tab` opens a new `crew_tab` immediately, skipping steps 1 and 2 of
  overflow.
- `--placement pane` with `--split-pane <id>` and `--direction` splits exactly that
  pane that way, as now, with no reference to the grid.
- `--direction` alone, with an auto pane, overrides the direction the slot implies.
  The crew lands in the pane the grid chose, split the way the flag says.

A crew placed by explicit flags **takes no slot**. It is recorded with `column` and
`row` unset, and the grid neither counts it nor splits it later. One hand placement
would otherwise corrupt every count after it.

## 8. What Herdr cannot do, and what it costs here

- **`pane move` inside one tab silently does nothing.** Re-nesting the dashboard
  therefore goes out to a scratch tab and back, one visible flicker. It is also the
  reason no part of this design tries to rearrange panes that already exist.
- **Splits are binary, one way, and pane-scoped.** `pane split` takes `right` or `down`,
  and the new pane always takes the far half of the *pane* named, never of its row or
  column. There is no insert before, no three way split, and no whole-row split, so a
  shape is only reachable as a sequence of splits. That is why fill order is fixed
  rather than chosen, and why column 1 of the captain's tab holds the captain alone
  (section 1).
- **A closed pane is found by failing.** If a recorded pane has gone, the split call
  fails and the recruit reports it, as today. The grid does not pre-check.

The grid only ever splits a pane it created, or the captain's own. It cannot touch a
stray editor pane sharing the tab, because it never looks at the tab. The mixed-tab
guard that exists today becomes unnecessary rather than merely unused.

## 9. Validation

Both keys must be a list of one or more whole numbers, each 1 or more, and
`captain_tab`'s first number must be 1: column 1 holds the captain alone.

A malformed array **fails the command** rather than falling back to the default. This
is a deliberate exception to the settings rule that a wrong-typed value is ignored: a
layout that silently ignores what you wrote is the bug people spend an afternoon on.
The error names the file it came from and the value it found.

```text
captain: [placement] captain_tab must be a list of whole numbers, each 1 or more,
like [1, 2, 2]. Got [1, 0, 2] from /Users/me/.captain/settings.toml: column 2 is 0.
```

```text
captain: [placement] crew_tab must be a list of whole numbers, each 1 or more,
like [2, 2]. Got "wide" from /Users/me/.captain/settings.toml.
```

```text
captain: [placement] captain_tab column 1 must be 1: it holds the captain alone, and
the captain's pane is never split in two. Got [2, 2] from
/Users/me/.captain/settings.toml. Put those crew in a later column, like [1, 2].
```

`config.lookup("placement", key)` returns the configured value; `layout.shape` then
checks that it is a non-empty list of positive integers, and that `captain_tab` leaves
column 1 to the captain, before using it.
