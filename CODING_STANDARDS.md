# Coding standards

How code in `src/captain_barbossa/` and `tests/` is written. Each rule has a stable id
for review citations ("CODING_STANDARDS.md S-2"). Setup, commits, branching and release
are in [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md); the module
ownership rule, the flat-package rule, comment rules and test-file naming are in
AGENTS.md and are not repeated here. Ruff (`E4 E7 E9 F I`, line length 100) and
`ruff format` already enforce import order, unused names and formatting: do not cite
those here.

## Design (SOLID, as it applies to a flat function-first package)

- **S-1 Single responsibility = one module owns one subject** (AGENTS.md). A diff that
  makes a module change for a second, unrelated reason (Divergent Change) violates it.
  Example: `usage` owns token/cost reading, `dashboard` only renders it.
- **S-2 Open/closed through data tables, not subclasses.** Extend behaviour by adding a
  row to the existing table: `models.MODELS`/`PROVIDERS`/`HOOKLESS`/`HOOK_DELIVERED`,
  `do.RUNNABLE`, `memory.REPO_RELATIONS`, `events.DELIVERY_HOOKS`. A new provider check
  written as a fresh `if provider == ...` cascade where a table already exists is a
  violation (Repeated Switches).
- **S-3 Dependency inversion = one door per external system, patched at the door.**
  Herdr is reached only through `runtime.herdr`; state paths only through `store`.
  Callers either call `runtime.herdr(...)` (tests patch `runtime.herdr`) or import the
  name (tests patch it where it is looked up, as with `sessions.herdr`). Do not add
  injectable parameters, protocols or wrappers to make a seam: the module attribute is
  the seam.
- **S-4 Liskov and interface segregation barely apply.** There is no inheritance beyond
  `CaptainError(Exception)` and `Session(namedtuple)`. Do not introduce a class
  hierarchy to have something for them to apply to.

## Patterns

- **P-1 Only patterns the code already earns.** Present today: table-driven dispatch
  (S-2), context-manager resource scope (`store.lock`, `protocol.checkpoint`), small
  value objects (`placement.Spot` frozen dataclass, `crew.Crew` dataclass,
  `sessions.Session`), single-writer functions (C-4), and `lru_cache(maxsize=1)` lazy
  singletons (`config.settings`, `config.defaults`, `models.pi_models`).
- **P-2 Tie-breaker: no unrequested abstraction wins over adding a pattern.** AGENTS.md
  "no unrequested abstractions" and the Speculative Generality smell override any
  pattern argument. One implementation means no interface, base class, factory,
  registry or strategy object. A class needs state that several functions share
  (`pane.Pane`, `placement.Placement`); otherwise write functions.
- **P-3 Deliberate ceilings are marked `ponytail:`** naming the limit and the upgrade
  path, as in `protocol.checkpoint` (one session lock) and `usage._start_refresh` (one
  price refresh per process).

## Coupling

- **C-1 Dependency direction.** Lower tiers never import higher ones at module level:
  1. `runtime` (stdlib only)
  2. `store`, then `config`, `events`, `sessions`, `pane`, `prompts`, `layout`, `models`
  3. `crew`, `protocol`, `memory`, `usage`, `instructions`, `inspection`, `placement`,
     `dashboard`, `do`, `onboarding`, `update_check`
  4. `agents`, then `cli` (only `cli` imports `agents`; nothing imports `cli` but
     `__main__`)

  Imports within a tier are fine if acyclic. `config` must not import `models` (its
  docstring says why).
- **C-2 No import cycles.** A function-local import is allowed only to break a cycle a
  module-level import would create, and needs a comment naming the cycle. Existing
  ones: `events.mail_context`/`receipt_for` -> `protocol`, `pane.inbox_line` ->
  `instructions`/`models`, `protocol.poll` -> `crew`. Adding another needs a reason the
  reviewer can check.
- **C-3 No reaching into privates.** A `_name` belongs to its module. Neither
  `from .x import _y` nor `x._y` from another module; promote the name (drop the
  underscore) if a second module needs it. Tests may patch privates.
- **C-4 Single doors stay single.** Do not add a second path for:
  - Herdr subprocesses: `runtime.herdr` (only `onboarding` may start `herdr server` and
    `exec` the binary, because no server exists yet).
  - state roots and project hashing: `store.temp_root`, `store.state_root`,
    `store.storage`, `store.state_storage`; never rebuild `<project hash>` paths.
  - marking mail `read`: `protocol.receipt`. Rendering mail for a model:
    `protocol.render`.
  - repo-memory relations: `memory.REPO_RELATIONS`.
  - defaults: `defaults.toml`, read through `config.lookup/text/flag/number`; never a
    literal fallback default in Python, never read while `cli.parser()` is built.
  - the version: `importlib.metadata` (`__init__.__version__`).
- **C-5 No hidden global mutable state.** Module-level values are constants
  (UPPER_CASE). Allowed exceptions: `lru_cache` singletons (P-1) and a one-shot process
  flag behind a `global` with a comment saying why it is process-wide
  (`store._warned_temp_state_root`, `usage._refreshed` under `_refresh_lock`). Anything
  else that must survive a call goes to disk via `store` (S-1, IO-1).
- **C-6 No re-export shims** (AGENTS.md). The one sanctioned exception is
  `memory.append_event`, kept because pre-0.30 hook settings bake that import string;
  copy its comment shape if another persisted string ever forces one.

## Errors

- **E-1 `CaptainError` is the one user-facing error.** Raise it with a message that
  tells the user what to do ("Run captain inside a Herdr workspace."). `cli.main`
  reports it; do not add new exception classes.
- **E-2 Translate at the boundary with `from exc`.** Wrap `OSError`, `ValueError`,
  `json` and `tomllib` failures into `CaptainError(...) from exc` where they are read
  (`store.read_json`, `config.read`, `runtime.herdr`).
- **E-3 Catch `runtime.HERDR_ERRORS`** around any Herdr or external-CLI subprocess
  call, not a bare `Exception`.
- **E-4 Broad `except Exception` only on best-effort side paths**, each with a comment
  saying why failure must not surface (`agents` dashboard launch, `update_check`
  version fetch, `usage._refresh_prices`). A silent `except Exception: pass` with no
  comment is a violation.

## Subprocess and JSON boundary

- **B-1 Every subprocess has a `timeout`** (or, for a streamed `Popen`, a monotonic
  deadline as in `inspection`) and an argv list (no `shell=True`); resolve the binary
  with `runtime.executable`. Untimed only: the interactive `update_check.upgrade` and
  the detached `herdr server` in `onboarding`.
- **B-2 Validate external JSON shape before use.** Copy `runtime.herdr`: check the
  envelope, check each nested object's type, and raise `CaptainError` with the raw
  output on mismatch. Never index a decoded payload you did not check.
- **B-3 Text crossing into a pane, mail or memory goes through `runtime.check_text`**,
  and a user-supplied path through `runtime.inside_project`.
- **B-4 Bounded reads of untrusted files** use `store.read_json(path, max_bytes=...)`
  (O_NOFOLLOW, regular file only), as `inspection` does via `sessions.read_session`.

## State on disk

- **IO-1 Replaced JSON documents are written with `store.write_json`/`write_text`**
  (temp file, fsync, `os.replace`). No `path.write_text` for state a reader may see
  half-written.
- **IO-2 Read-modify-write of shared state happens under `store.lock`** for the whole
  cycle; for `protocol.json` that is `protocol.checkpoint`. Never read outside the
  lock and write inside it.
- **IO-3 Append-only logs** (`events/<crew>.jsonl`) are written one JSON line per
  `os.write` with `O_APPEND | O_NOFOLLOW`, as `events.append_event` does.
- **IO-4 Private by default.** Directories under the state/temp roots go through
  `store.private_dir` (0700, no symlink, owned by the user); files created there are
  `chmod 0o600`.
- **IO-5 Read-only paths do not create.** A read command (`memory show`, `inspect`)
  resolves with `store.read_storage` and never mkdirs, chmods, locks or migrates.

## Naming

- **N-1 snake_case** for functions, variables, parameters, modules and `.py` files;
  CapWords for classes; UPPER_CASE for module constants; a leading `_` for module
  privates (C-3). Ruff does not check this here (`N` is not selected).
- **N-2 Names say what the value is, not who calls it** (AGENTS.md module rule, applied
  to functions): `protocol.receipt`, `store.read_cursor`, `crew.Crew.is_dismissed`.
  Booleans read as predicates (`is_dismissed`, `in_workspace`).
- **N-3 Not snake_case on purpose; do not flag:**
  - environment variables: `CAPTAIN_MEMORY_ROOT`, `CAPTAIN_STATE_ROOT`, `HERDR_PANE_ID`.
  - CLI flags and subcommands: kebab-case (`--no-dashboard`, `--refresh-prices`).
  - JSON fields owned by another program: Claude hook output (`hookSpecificOutput`,
    `hookEventName`, `additionalContext`), native hook event names (`SessionStart`,
    `UserPromptSubmit`, `Stop`), Herdr payload keys.
  - Herdr ids and refs (`w1:p2`, `w1:t1`) and the `c-<session>-<crew>` agent names.
  - the JavaScript embedded in `pi_captain` (camelCase).
  - `defaults.toml` keys are snake_case, but they are user API: renaming one is a
    breaking change, not a style fix.

## Typing

- **T-1 No annotations on functions.** The package is unannotated and has no type
  checker; do not add partial hints in a diff. Dataclass fields are annotated because
  `@dataclass` requires it, using `X | None` (py311).

## Tests

- **TS-1 Never touch the real Herdr, network, or memory roots.** Subclass
  `tests.home_isolation.SessionCase` for session state; patch `runtime.herdr` (or the
  importing module's name, S-3), `urllib.request.urlopen` and `subprocess.run`.
- **TS-2 Patch time, do not sleep.** Patch the module's `time` (`pane.time`,
  `protocol.time`) for timeouts and polling.
- **TS-3 Changed behaviour gets one focused regression test** (AGENTS.md) that fails
  without the change; assert on files and payloads written, not on log prose.
- **TS-4 Instruction text is budgeted.** A diff to `instructions.py` that grows generated
  text must keep `tests/test_instruction_size.py` passing without raising its limit.
