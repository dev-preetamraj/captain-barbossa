# TODOs

- **Gap:** Captain role instructions in `src/captain_barbossa/instructions.py` only force crew delegation for "edit, file write, build, test, or debug". Research, market analysis, and web investigation are not listed, so the captain handles them solo. Widen the delegation rule to any substantive work, not just code changes. Requires matching wording change in crew ruleset and a regression test.

- **Auto-placement:** `src/captain_barbossa/layout.py` `pick_auto_split` caps the captain tab at 2 crew panes and a crew tab at 4, regardless of terminal size. A large screen still gets pushed into a new tab.
  - Wanted: drop the fixed counts and let geometry decide. Split vertically while width allows, horizontally while height allows, using the MIN_WIDTH/MIN_HEIGHT feasibility and balance scoring `pick_split` already has.
  - The captain pane must stay undivided: remove the current last-resort branch that splits the captain pane down.
  - Needs regression tests covering a wide screen fitting more than 2 crew in the captain tab, and the captain pane never being chosen.

- **Codex cheap tier (resolved 2026-09-20):** ChatGPT-account Codex failed immediately on gpt-5.3-codex-spark model.
  - Observed live: a cheap codex crew got `{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The gpt-5.3-codex-spark model is not supported when using Codex with a ChatGPT account."}}` on its first turn, had to be retiered to gpt-5.6-luna by hand.
  - The local Codex picker no longer lists gpt-5.3-codex-spark or gpt-5.4-mini at all (only gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5); both ids 400 on this ChatGPT account.
  - Fix: dropped both ids from `MODELS["codex"]` in `src/captain_barbossa/models.py`, retiered `cheap` -> gpt-5.6-luna, `mid` -> gpt-5.6-sol, `strong` -> gpt-6-astra (gpt-5.6-terra and gpt-5.5 stay selectable by name, just not tier defaults). Verified `codex exec -m gpt-5.6-luna` launches and responds with no 400.
  - Regression test updated in `tests/test_model_tiers.py`.

- **Context percentage self-calibration:** derive the dashboard's `CONTEXT` denominator from our own event log instead of scanning `~/.claude/projects`.
  - Verified against claude 2.1.278: a `PreCompact` hook fires and its payload is `{session_id, transcript_path, cwd, prompt_id, hook_event_name, trigger, custom_instructions}` - it carries `trigger` ("auto"/"manual") but no token count, so adding it alone buys nothing.
  - `PostCompact` fires after the boundary record is written and carries `trigger`, `compact_summary` and `transcript_path`. A `PostCompact` hook could read `compactMetadata.preTokens` out of that one named transcript and append it to `events/<crew>.jsonl`, giving a per-model ceiling from our own payload with no machine-wide scan.
  - Worth doing only once automatic compactions actually occur: there are currently zero on this machine across 223 transcripts, so there is nothing for either path to read.

- **Codex crew startup false negative:** `Pane.submit_task` in `src/captain_barbossa/pane.py` reports a Codex crew as never started while it is visibly working.
  - Observed live 2026-09-20: a codex crew was created, the task reached its input box, and Codex was working on it, yet `create_crew` raised "`<agent>` did not start working after the task was submitted 2 times" and the command exited 1. The crew went on to finish the task normally.
  - The check is `task_landed` -> `settled_status`, which for codex treats a pending draft as idle (`provider == "codex" and self.draft_pending()`). Working Codex appears to read idle through that path; confirm against `herdr agent list` output for a codex agent mid-turn before changing anything.
  - Two costs, not one: the `attempts=2` loop already re-sends `herdr agent prompt` before failing, and the error text then tells the reader to resend a third time. Following it double-sends the task, which is worse than the false negative.
  - Fix direction: for codex, treat a Codex `notify` event on `events/<crew>.jsonl` as proof the turn started and stop polling the pane; failing that, verify the draft heuristic against a live working pane and stop re-prompting until the first send is known not to have landed.

- **`captain move NAME`:** crew are stuck in the tab they were recruited into; a command to move one is missing.
  - Verified: `herdr pane move` does work across tabs, so a crew pane can be relocated after the fact. Within a single tab it silently no-ops (no error, no change), which is the same reparenting limit `renest_dashboard` already works around.
  - Wanted: `captain move NAME [--tab TAB]` reusing the resolver `captain focus` uses, updating the crew record's `tab`/`pane`, refreshing the tab label via `Crew.tab_label`, and re-nesting the dashboard when the captain tab is involved.
  - Because the within-tab case no-ops silently, the command must detect it and say so rather than reporting a move that did not happen.

- **`memory.session()` needs a live pane to open an existing session:** `src/captain_barbossa/memory.py:371` dereferences `pane["workspace_id"]` on the existing-session path, to check the session belongs to this Herdr workspace.
  - Effect: nothing can open a session programmatically without a real Herdr pane - a script, a test harness, or any out-of-pane tool has to fabricate a pane dict. `create=False` reads should not require one.
  - Fix direction: accept `pane=None` and skip the workspace check when there is no pane to check against (the project match still holds), rather than inventing a placeholder workspace id.
