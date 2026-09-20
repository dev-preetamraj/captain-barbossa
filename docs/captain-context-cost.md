# Captain context cost: what the residual actually is

Measurement-only report. No code changed.

A previous note put a captain's first API call at 56,485 tokens and assigned 63.9%
of it (36,094 tokens) to a RESIDUAL labelled "active tool JSON schemas + per-message
API framing + tokenizer error". That label was inference. This report replaces it
with measurement.

**Headline:** the residual was real, but it was not mostly tokenizer error and not
mostly framing. It is dominated by **built-in tool schemas plus the deferred-tool
index (16,150 tokens, measured)** and by **interactive-session context that print
mode never loads (22,232 tokens, measured as a lump)**. Separately, the earlier
analysis undercounted every one of its named buckets by ~55%, because tiktoken
`o200k_base` undercounts Claude's real tokenizer by a measured factor of **1.48-1.57**.
That undercount is what inflated the residual.

## How everything here was measured

Every token number below is a **real API count**, not an estimate. Method:

```
claude -p "say ok" --output-format json <flags>
```

`--output-format json` returns the API's own `usage` block. Total input for the call
is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Changing
one flag at a time and differencing the totals gives the real marginal cost of each
context component.

- Host: this machine, `claude` 2.1.278, model `claude-opus-5[1m]`, cwd
  `/Users/preetam/code/projects/captain-barbossa`, 2026-09-20.
- Captain-equivalent runs use the real launch flags from
  `src/captain_barbossa/instructions.py:141` (`native_args`): `--append-system-prompt
  <role instructions>` plus `--settings <hooks json>`.
- Totals are reproducible. Identical configs re-run to the exact same total
  (`--strict-mcp-config` gave 29,341 twice; `--tools Workflow` gave 17,119 twice;
  the captain instruction delta came out at 2,309 on four independent runs).
- One known noise source: MCP servers connect asynchronously, so a bare run lands on
  either 30,255 or 31,944 depending on whether the connectors won the race. Every
  MCP-sensitive number below is stated against `--strict-mcp-config`, which is stable.

Interactive totals come from the real session transcripts in
`~/.claude/projects/-Users-preetam-code-projects-captain-barbossa/*.jsonl`, reading
`usage` on the first assistant message.

An attempt to capture the literal HTTP request body through a logging reverse proxy
was **blocked by the auto-mode traffic-redirection classifier**. That is why the
per-component split comes from differencing real API counts rather than from reading
the wire format. Consequences are flagged as UNMEASURED where they matter.

## 1. The residual, identified

### Raw measurement ladder

Each row is one real API call. The delta column is the cost of the thing added on
that row.

| # | Configuration | Total input tokens | Delta | What the delta buys |
|---|---|---|---|---|
| 1 | cwd `/tmp`, `--tools ""`, `--disable-slash-commands`, `--strict-mcp-config` | 5,870 | - | Claude Code base system prompt + env + user-level `CLAUDE.md` + the user message |
| 2 | same, cwd = this repo | 8,498 | +2,628 | project `CLAUDE.md` + `MEMORY.md` + injected git status / repo context |
| 3 | + default built-in tools | 24,648 | **+16,150** | **all built-in tool schemas + the deferred-tool index** |
| 4 | + skills enabled | 29,341 | +4,693 | the 120-entry skills listing |
| 5 | + MCP servers (2 connected, all deferred) | 31,944 | +2,603 | deferred MCP tool index |
| 6 | + captain role instructions | 34,253 | +2,309 | `instructions.agent_instructions(...)`, 6,462 bytes |
| 7 | real interactive captain first call (transcript) | 49,447 - 56,673 | +15,194 to +22,420 | interactive-only context, see §1.3 |

### 1.2 Active vs deferred tools, and the real schema sizes

Deferred tools are **not free here.** Measured, not assumed.

Per-tool cost, measured as `--tools <name>` alone minus the 8,498 no-tools floor
(`--strict-mcp-config --disable-slash-commands` throughout):

| Tool | Real tokens |
|---|---|
| Workflow | **8,621** |
| Agent / Task (same tool) | 1,972 |
| Grep | 1,655 |
| Bash | 1,451 |
| Read | 987 |
| Edit | 727 |
| WebFetch | 711 |
| WebSearch | 689 |
| Glob | 656 |
| Write | 615 |

`Workflow` is the single most expensive tool in Claude Code by a factor of four. It
reproduced exactly (17,119 total on two separate runs).

Three observations that only fall out of measurement:

- **Sub-additive.** `--tools Read,Bash` costs 2,059, not the 2,438 the two individual
  numbers predict. 379 tokens of framing are shared.
- **The whole default set costs less than the sum of its parts.** All built-in tools
  together cost 16,150, but the ten measured individually already sum to 18,084.
  Claude Code defers tools once the set is large, so most tools are present as an
  index entry (a name and one line) rather than a full JSON schema. The 16,150 is
  therefore mostly **index**, not mostly **schema**.
- **`--disallowedTools` saves nothing.** `--disallowed-tools Bash` measured 25,129 and
  `--disallowed-tools Artifact` measured 24,648, against a 24,648 baseline; the
  Artifact run was byte-identical to baseline (fully cache-read). Denying a tool
  blocks it at runtime and leaves its schema in the request. Only `--tools` removes it.

**Re MCP and "234 tools - 0 tokens":** that claim is **wrong as an absolute**.
Measured, turning MCP off with `--strict-mcp-config` saves **2,603 tokens** (31,944 →
29,341). What `/context` means is that deferred MCP tools cost no *schema*; they still
cost a name and a description line in the index. Caveat: only 2 of the 5 configured
servers (Claude Docs, Google Drive, ~19 tools) had connected in the measured runs. The
cost of the full 234-tool index is **UNMEASURED** and is higher than 2,603.

### 1.3 The interactive-only 22,232

Print mode and an interactive captain do not load the same context. Measured by
asking a print-mode session to enumerate its own tools, it has **11 active + 14
deferred**. An interactive session in this project has **14 active + ~90 deferred
system/plugin tools + 234 deferred MCP names**, plus MCP server instruction blocks,
the agent-type listing, and interactive system-reminders.

The aggregate is measured (56,485 − 34,253 = 22,232). **The split inside it is
UNMEASURED.** `--tools` cannot address `Artifact`, `AskUserQuestion` or `SendFeedback`
(they are not in the selectable built-in set), and interactive mode cannot be driven
headlessly, so no clean single-variable difference was available. Extracting the
schemas from the compiled binary was attempted and abandoned: `strings` recovers the
`Artifact` description only in truncated fragments, which would have meant estimating.

## 2. Tokenizer error, quantified

Measured by the **slope method**: put a text of known `tiktoken o200k_base` length
into `--append-system-prompt` at two sizes and difference the real API totals. The
slope is the true tokens-per-tiktoken-token ratio; fixed framing cancels out.

| Text | tiktoken `o200k_base` | Real marginal tokens |
|---|---|---|
| captain instructions ×1 | 1,461 | 2,309 |
| captain instructions ×4 | 5,844 | 9,209 |
| captain instructions ×8 | 11,688 | 18,409 |

- Slope 1x→4x: `(9,209 − 2,309) / (5,844 − 1,461)` = **1.574**
- Slope 4x→8x: `(18,409 − 9,209) / (11,688 − 5,844)` = **1.574**
- Perfectly linear, so the model is real.
- **Fixed framing intercept: `2,309 − 1.574 × 1,461` = 9 tokens.**

That intercept is the important one. **Per-block API framing costs ~9 tokens, not
thousands.** The earlier residual label named "per-message API framing" as a
contributor; measured, it is negligible.

Second text, plain prose (`README.md` ×4, 23,400 tiktoken tokens, 34,637 real):
ratio **1.480**. So the factor is text-dependent, roughly **1.48-1.57**.

Independent corroboration: Claude Code's own `/context` estimator sits at the same
ratio against tiktoken for the three memory files, which is what you would expect if
it is modelling the real tokenizer.

| File | tiktoken | `/context` | ratio |
|---|---|---|---|
| `~/.claude/CLAUDE.md` | 228 | 349 | 1.53 |
| project `CLAUDE.md` | 1,020 | 1,600 | 1.57 |
| `MEMORY.md` | 272 | 433 | 1.59 |

**Correction factor: 1.55 ± 0.05.** tiktoken `o200k_base` undercounts Claude's real
input tokens by about 35% of the true count.

## 3. The corrected table

### 3.1 Bottom-up, all API-measured (the 56,485 call)

| Bucket | Tokens | Share | Source |
|---|---|---|---|
| Base system prompt + env + user `CLAUDE.md` | 5,870 | 10.4% | measured, row 1 |
| Project `CLAUDE.md` + `MEMORY.md` + git/repo context | 2,628 | 4.7% | measured, row 2 |
| **Built-in tool schemas + deferred-tool index** | **16,150** | **28.6%** | measured, row 3 |
| Skills listing (120 skills) | 4,693 | 8.3% | measured, row 4 |
| MCP deferred index (2 of 5 servers) | 2,603 | 4.6% | measured, row 5; lower bound |
| Captain role instructions | 2,309 | 4.1% | measured, row 6 |
| Interactive-only context | 22,232 | 39.4% | measured as a lump; internal split UNMEASURED |
| Tokenizer error | **0** | 0% | every number above is a real API count |
| Per-message API framing | ~9 per block | ~0% | measured intercept, §2 |
| **Total** | **56,485** | 100% | |

### 3.2 Restating the original residual

The earlier analysis counted its named buckets with tiktoken, so each was ~1.55x too
small. Applying the correction (assumption stated: its non-residual buckets summed to
100% − 63.9% = 20,391 tiktoken-derived tokens):

| | Tokens | Share |
|---|---|---|
| Original named buckets (tiktoken) | 20,391 | 36.1% |
| Same buckets, ×1.55 correction | 31,606 | 56.0% |
| **Residual after tokenizer correction** | **24,879** | **44.0%** (was 63.9%) |
| ...of which built-in tool schemas + index (measured) | 16,150 | 28.6% |
| ...of which MCP index (measured) | 2,603 | 4.6% |
| **Genuinely unexplained** | **6,126** | **10.8%** |

The residual drops from 63.9% to 10.8%. Roughly a third of the original residual was
pure tokenizer undercount; most of the rest was tool schemas and indexes.

## 4. What the user can change

All savings are real measured deltas on this machine. They are print-mode deltas; the
same components are present interactively, so they carry, but the interactive totals
they apply to are larger.

### User-controllable

| # | Change | Saving | Exact setting |
|---|---|---|---|
| 1 | Trim built-in tools to what a captain uses | **11,945** | `--tools Bash,Read,Edit,Write,Grep,Glob` (24,648 → 12,703) |
| 1a | ...of which just dropping `Workflow` | 8,621 | omit `Workflow` from `--tools` |
| 2 | Turn off skills | **4,693** | `--disable-slash-commands`, or uninstall unused plugins |
| 2a | ...structurally: drop unused plugin bundles | part of the 4,693 | `/plugin` , or remove from `enabledPlugins` in `~/.claude/settings.json`. Currently installed: `sales` (36 skills), `marketing` (8), `design` (7), `operations` (9), `product-management` (8), `clerk` (10), `ponytail`. `sales` alone is the largest block. |
| 3 | Turn off MCP servers | **2,603** (lower bound) | `--strict-mcp-config`, or disconnect unused connectors on claude.ai. Measured with 2 of 5 servers connected; the true figure for all 234 tools is UNMEASURED and larger. |
| 4 | Trim project `CLAUDE.md` / `AGENTS.md` | up to 2,628 | that delta also contains git status, so the editable part is ~1,600 (project `CLAUDE.md`) |
| 5 | Trim captain role instructions | up to 2,309 | `src/captain_barbossa/instructions.py` — the only Captain Barbossa-controlled item on this list |
| — | `--disallowedTools` | **0** | measured no-op; does not remove schemas |
| — | `--restricted` | 12,960 | measured, but removes `Bash` and all code-running tools. Not viable for a captain. |

Combined, measured end to end: a captain-equivalent session goes from **34,253 →
15,012 tokens** (`--tools Bash,Read,Edit,Write,Grep,Glob --disable-slash-commands
--strict-mcp-config` plus the captain's `--append-system-prompt`). That is a **56%
cut**, and against the real interactive 56,485 it is a **73% cut**.

### Only Anthropic can change

- Base system prompt, 5,870 tokens.
- The deferred-tool index format (a name + a description line per tool).
- `Artifact`, `AskUserQuestion`, `SendFeedback` schemas: not selectable via `--tools`,
  so not removable by the user.
- The 22,232-token interactive-only block.
- The `Workflow` tool's 8,621-token description, if it is to stay active by default.

## 5. The irreducible floor

| Floor | Tokens | What it is |
|---|---|---|
| Absolute measured floor | **5,870** | No tools, no skills, no MCP, outside any project. Unusable: the agent cannot read or edit anything. |
| Usable floor, no captain | **12,703** | Six file/shell tools, no skills, no MCP, in this repo. |
| **Usable floor, captain** | **15,012** | The above plus the captain role instructions. **A captain cannot go below this and still be a captain.** |

Of that 15,012, the parts nobody can remove are the 5,870 base and the ~6,100 of
schemas for the six tools a captain needs. The rest is the repo's own `CLAUDE.md` and
the captain's role instructions, both of which are editable text.

Caveat on the floor: these are print-mode figures. An interactive session adds the
22,232-token block in §1.3, and how much of that survives tool trimming is
**UNMEASURED**.

## Top three user-actionable savings

1. **`--tools Bash,Read,Edit,Write,Grep,Glob` — 11,945 tokens per call.** Most of it
   is the `Workflow` tool alone at 8,621.
2. **`--disable-slash-commands`, or uninstall unused plugin bundles — 4,693 tokens.**
   The `sales` bundle's 36 skills are the largest single block.
3. **`--strict-mcp-config`, or disconnect unused connectors — 2,603 tokens and rising**
   with each server that connects.
