# Placement: declared tab shapes

Crew placement is declared, not inferred. You write the shape of a tab and the grid
fills it in a fixed order. There is no scoring, no measuring, and no negotiation: the
declared slot is created, however small it lands.

```toml
[placement]
captain_tab = [1, 2, 2]
crew_tab = [2, 2, 3]
```

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

- `captain_tab` shapes the tab the captain launched in. Column 1 row 1 is the captain.
- `crew_tab` shapes every other tab.
- Crew a tab holds is the sum of its columns, minus the captain: `[1,2,2]` holds four,
  `[2,2,3]` holds seven.
- The dashboard is not a slot. It nests under the captain inside column 1 and is
  invisible to the grid.

### Capacity above 1 in column 1

`captain_tab = [2, 2, 2]` means the captain plus one crew stacked beneath it, then two
columns of two: five crew in total.

The dashboard stays the bottom row of column 1, so a crew arriving in column 1 splits
the lowest *slot* pane in that column downward (the captain when it is the first) and
the dashboard is then re-nested below it. Re-nesting is not free: Herdr cannot move a
pane inside its own tab (section 8), so each column 1 insert costs the same scratch
tab round trip and visible flicker the dashboard already pays on a crew split.

`captain_tab = [1, ...]` avoids all of that and is the recommended default. A capacity
above 1 there is supported because the model should not have a special case, not
because it is a good idea.

## 2. Schema

| Key | Type | Default | Means |
| --- | --- | --- | --- |
| `captain_tab` | list of ints >= 1 | `[1, 2]` | Shape of the tab the captain is in. |
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

Herdr halves a pane unless `--ratio` says otherwise, and ratio is the share the split
pane keeps. Halving makes `[1, 2, 2]` come out 1/2, 1/4, 1/4 rather than in thirds.
Since the shape is known up front, the even ratio is arithmetic:

- Opening column `c` of `n`: `ratio = 1 / (n - c + 2)`.
- Opening row `r` of a column holding `k`: `ratio = 1 / (k - r + 2)`.

For `[1, 2, 2]`: column 2 opens at `1/3`, column 3 at `1/2`, giving three equal
columns; each row 2 opens at `1/2`. The arithmetic is exact only for a tab filled in
order from empty. After dismissals and refills the panes land wherever the surviving
splits leave them, which the absolute grid already accepts.

A direction forced by `--direction` drops the ratio, because the arithmetic only
describes the split the shape asked for. Herdr then halves the pane as it would have
anyway.

## 5. Bookkeeping, from the roster

Placement reads the session roster and nothing else. It never calls `pane layout`,
never measures a pane, and never looks at what Herdr reports. That is what makes it
deterministic: the same roster and the same shape give the same answer, whatever order
Herdr would have listed panes in.

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
  therefore goes out to a scratch tab and back, one visible flicker. This is the only
  reason `captain_tab` column 1 capacity above 1 is expensive, and the reason no part
  of this design tries to rearrange panes that already exist.
- **Splits are binary and one way.** `pane split` takes `right` or `down`, and the new
  pane always takes the far half. There is no insert before and no three way split, so
  a shape is only reachable as a sequence of splits, which is exactly why fill order is
  fixed rather than chosen.
- **A closed pane is found by failing.** If a recorded pane has gone, the split call
  fails and the recruit reports it, as today. The grid does not pre-check.

The grid only ever splits a pane it created, or the captain's own. It cannot touch a
stray editor pane sharing the tab, because it never looks at the tab. The mixed-tab
guard that exists today becomes unnecessary rather than merely unused.

## 9. Validation

Both keys must be a list of one or more whole numbers, each 1 or more.

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

`config.lookup("placement", key)` returns the configured value; placement then checks
that it is a non-empty list of positive integers before using it.
