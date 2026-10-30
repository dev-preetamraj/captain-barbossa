# Memory

Captain stores graph relationships and launch metadata in four scopes.
Session and project memory live outside the repository in the durable state
root, so both survive OS temp cleanup; repo memory is committed with the code
and shared by the team; user memory holds your own preferences for every project:

```text
<project>/.captain/graph.json     # curated --scope repo facts, committed

~/.local/state/captain-barbossa/user/graph.json  # --scope user preferences, all projects

~/.local/state/captain-barbossa/<hash of project path>/
  graph.json                      # explicit --scope project facts
  sessions/<session-id>/
    session.json                  # workspace and crew references
    graph.json                    # this session's memory only
    protocol.json                 # assignments, questions, delivery acknowledgements
    events/<crew>-<incarnation>.jsonl  # native events
    lifetimes/<incarnation>.*          # authoritative managed-writer ownership/exit receipts
    native-history/<crew>-<incarnation>.json # crew-only native identity/cleanup journal
    background/<crew>-<incarnation>.*  # background run record, lock, bounded output, report

```

Sessions created by an older captain live under
`<OS temp>/captain-barbossa-<uid>/<hash of project path>/sessions/` and are opened
there in place, never moved; one session id has exactly one directory, and an id
found in both roots is refused as `[duplicate-session]`. Background crew need a
durable session.

## Repo scope

`--scope repo` writes `.captain/graph.json` inside the checkout, beside
`.captain/settings.toml`, so a decision recorded once reaches every teammate
through version control. It is curated, not a dump: architectural decisions,
methods, conventions, and their rationale, nothing else.

You choose the content; the code fixes the shape. Only an explicit
`memory add --scope repo` writes it, with no inference, extraction, or
summarising step anywhere in the write path. Session facts are never
promoted into it, nothing is auto-committed, and crew reports, lifecycle
events, timestamps, and machine paths stay out.

```sh
captain memory add "placement" decided "declared tab shapes" \
  --scope repo --because "even ratios must survive a dismissal"
captain memory show --scope repo
```

The relation comes from one closed vocabulary (`memory.REPO_RELATIONS`, the
only place it is defined); anything else is refused with the allowed set:

| Relation | Records |
| --- | --- |
| `decided` | an architectural decision |
| `method` | how the team does something |
| `convention` | a rule the code follows |

`--because` is required and carries the rationale onto the link. Every field
is capped at 300 characters and refused rather than spilled to a note file,
because a value that long is a transcript, not a fact.

The file is deterministic. Node ids hash the label alone, nodes and links are
sorted, and nothing timestamped, random, or machine-specific is stored, so
the same facts always produce byte-identical bytes regardless of the order
they were written in. Re-adding a fact is an idempotent no-op, not a second
row.

Changing a recorded fact is an explicit supersede, never newest-wins. A write
whose subject and relation are already on record is refused; `--supersede`
replaces that one row (and drops the object it orphans), leaving exactly the
bytes recording the new fact first would have produced:

```sh
captain memory add "placement" decided "free-form splits" \
  --scope repo --because "the shape got in the way" --supersede
```

Reads leave the committed file untouched: it is never migrated or rewritten
in place, so pulling a teammate's graph cannot rewrite your checkout. Its
lock lives in the state root, so no lock file is ever committed. A
`.captain/graph.json` that is not a graph fails loudly with the path to fix.

Crew are required to read `memory show --scope repo` at startup; the CLI
enforces repo scope for crew. Only the captain reads session and project
memory, which holds other crew's assignments.

`$XDG_STATE_HOME` is honored in place of `~/.local/state` when set. Git
repositories use the captain checkout root as project identity; other directories
use the launch directory. Crew inherit their captain's
project and session; only explicitly saved project facts carry into other sessions. Set
`CAPTAIN_MEMORY_ROOT` before starting captain to redirect both roots at once
(used for test isolation and custom retention); it must point outside the
project.

## User scope

`--scope user` holds your standing preferences for every project, such as how
commits are written or how terse replies should be. It is stored once, in
`user/graph.json` under the durable state root (the same `CAPTAIN_STATE_ROOT`,
`CAPTAIN_MEMORY_ROOT` and `$XDG_STATE_HOME` overrides apply) with its own lock,
and every captain launch, resume and `recover` loads all of it, so you never
repeat a preference.

```sh
captain memory add "commits" "style" "conventional, no AI attribution" --scope user
captain memory add "commits" "style" "gitmoji" --scope user --supersede
captain memory forget "commits" "style" --scope user
captain memory show --scope user
```

Each subject and relation holds one value: re-adding the same preference is a
no-op, and a different value is refused unless you pass `--supersede`. Values are
capped at 300 characters, and the total, measured as the launch digest serialises
it, at `[memory] user_limit` characters (4000 by default); a write past either cap is
refused rather than cut later.

Preferences lead the digest as its `preferences` key and are read apart from the
other scopes, so a broken session, project or repo graph cannot hide them and the
digest's launch truncation never cuts them. A broken user graph shows as
`"preferences": "unavailable: ..."` while the rest of the digest still loads. A
project that should not get them sets `[memory] user_enabled = false` in its
`.captain/settings.toml`; `show --scope user` still lists them there.

The captain follows a preference unless the current request or the repo's rules
say otherwise, and a preference never grants permission. Crew can neither read nor
write user scope: their `show` is pinned to repo scope and every other user-scope
command is refused.

## Add, show, and query

```sh
captain memory add "rate limiter" "uses" "per-user windows"
captain memory add "test command" "is" "python -m unittest" --scope project
captain memory add "placement" decided "declared tab shapes" --scope repo \
  --because "even ratios must survive a dismissal"
captain memory init --apply
captain memory show
captain memory show --scope repo
captain memory query "rate limiter"
captain memory path
captain memory path --scope project
```

Captain launches, `--resume` and `--session` inject this recovery digest into the
captain's system prompt automatically; run `captain recover` after compaction. The
captain saves user requests, scope and decisions to session memory unasked, so a
resumed captain knows them. The JSON digest holds user preferences first, then the
session title, crew reports from done assignments, session, project and repo
memory, active assignments and owned
paths, pending questions, wait deliveries and queued crew mail. Each section (and each memory scope) includes
at most 25 entries with full text; `omitted` counts the remaining entries explicitly.
Saved deliveries retain their IDs; queued notices have `delivery_id: null` until
`wait` surfaces them. Recovery reads stored state only, without Herdr, native-event
polling, acknowledgements or writes. Use `wait --all --json --timeout 0` for fresh
native notifications and the existing scoped readers for omitted detail.

`memory show` prints the most recent relationships as `[scope] [subject,
relation, object]`; `--all` shows every link, `--json` dumps the raw graph,
and `--scope` narrows the output to one scope. Text output caps each subject,
relation, and object at 300 characters, including with `--all`; stored values
and explicit `--json` output remain intact. Recent session rows fill the
25-row default, with 5 rows reserved for project and 5 for repo so durable
facts are never crowded off the end; user rows follow in full, since their total
is capped when written. Default add scope is `session`; use
`--scope project` only for facts that should survive into future sessions,
`--scope repo` only for team facts worth committing, and `--scope user` only for
preferences that hold in every project. `--because` applies to `--scope repo`
alone and `--supersede` to repo and user; both are refused elsewhere.

`show` and `path` are pure reads: no directory creation, chmod, locks, migration,
snapshot, or graph rewrite. `show --scope ... --json` reads only that scope.
When the durable project graph is absent, `show` reads a legacy temp-root project
graph in place; it never copies it. `path --scope session|project|repo|user` prints
the selected directory, defaulting to session. Repo/project/user reads need no session;
session reads require existing matching metadata. Both commands bypass Herdr
pane lookup. Full `--json` and `--all` output remain unbounded; use
`inspect state SCOPE [PATH]` for bounded stored-file
inspection. Crew can inspect only repo state.

Protocol state stays session-local and is separate from graph memory. Questions,
reports, original tasks, and message delivery records survive captain restarts;
legacy crew are not silently adopted into the protocol. See
[crew-lifecycle.md](crew-lifecycle.md) for explicit completion and acknowledgements.

`memory query TEXT` performs case-insensitive literal substring search over stored
subjects, relations, and objects in session, project, repo, and user memory. It prints
matching rows with scope tags, without a model, external tool, snapshot, or cleanup.
Use `memory query -- '--all'` to search text beginning with a dash. Search covers
stored labels, not the full text spilled into note files.

## Prune old sessions

Session directories accumulate as sessions end. `captain memory prune` (also
run automatically, silently, and best-effort at every launch) removes
directories where nothing has been touched for `--older-than` days (7 by
default) and Herdr reports no live agent in their panes; when Herdr is
unreachable, only directories twice that age are removed. Both the durable and
the legacy temp root are pruned. A session that still holds unresolved work is
kept at any age: an unfinished assignment or an unacknowledged outcome of a crew
that is not dismissed, protocol state that cannot be read, unresolved artifacts,
an unsettled managed lifetime, or native-history cleanup not recorded `complete`.
Native cleanup first requires authoritative writer shutdown and freezes usage, then
considers only session identities recorded for that crew incarnation—never captain or
unrelated history. Missing manifests are reported unsupported and cause no deletion;
missing hooks, unknown writers, ambiguous identity, unsupported/partial providers, and
shared sidecar uncertainty leave non-complete journals that retain the session instead
of triggering broad deletion. The current session, the durable project graph, and the
committed repo graph are never removed.

```sh
captain memory prune
captain memory prune --older-than 30
```
