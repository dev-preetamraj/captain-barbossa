# Memory

Captain stores graph relationships and launch metadata in three scopes.
Session and project memory live outside the repository, split so durable
project facts survive OS temp cleanup while ephemeral session state does
not; repo memory is committed with the code and shared by the team:

```text
<project>/.captain/graph.json     # curated --scope repo facts, committed

~/.local/state/captain-barbossa/<hash of project path>/
  graph.json                      # explicit --scope project facts

<OS temp>/captain-barbossa-<uid>/<hash of project path>/
  sessions/<session-id>/
    session.json                  # workspace and crew references
    graph.json                    # this session's memory only
    protocol.json                 # assignments, questions, delivery acknowledgements
    events/<crew>-<incarnation>.jsonl  # native events; legacy crew omit incarnation
```

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
repositories use their checkout root as project identity; other directories
use the launch directory. Crew inherit their captain's project and session;
only explicitly saved project facts carry into other sessions. Set
`CAPTAIN_MEMORY_ROOT` before starting captain to redirect both roots at once
(used for test isolation and custom retention); it must point outside the
project.

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

`memory show` prints the most recent relationships as `[scope] [subject,
relation, object]`; `--all` shows every link, `--json` dumps the raw graph,
and `--scope` narrows the output to one scope. Text output caps each subject,
relation, and object at 300 characters, including with `--all`; stored values
and explicit `--json` output remain intact. Recent session rows fill the
25-row default, with 5 rows reserved for project and 5 for repo so durable
facts are never crowded off the end. Default add scope is `session`; use
`--scope project` only for facts that should survive into future sessions,
and `--scope repo` only for team facts worth committing. `--because` and
`--supersede` apply to `--scope repo` alone and are refused elsewhere.

`show` and `path` are pure reads: no directory creation, chmod, locks, migration,
snapshot, or graph rewrite. `show --scope ... --json` reads only that scope.
When the durable project graph is absent, `show` reads a legacy temp-root project
graph in place; it never copies it. `path --scope session|project|repo` prints
the selected directory, defaulting to session. Repo/project reads need no session;
session reads require existing matching metadata. Both commands bypass Herdr
pane lookup. Full `--json` and `--all` output remain unbounded; use
`inspect state SCOPE [PATH]` for bounded stored-file
inspection. Crew can inspect only repo state.

Protocol state stays session-local and is separate from graph memory. Questions,
reports, original tasks, and message delivery records survive captain restarts;
legacy crew are not silently adopted into the protocol. See
[crew-lifecycle.md](crew-lifecycle.md) for explicit completion and acknowledgements.

[Graphify](https://graphify.com/docs/cli) is optional: install it with
`uv tool install graphifyy` to enable `memory query`, which runs against an
isolated snapshot of session, project, and repo memory that is removed afterward.
Relationships can still be added and read with `memory add`/`show` without it.

## Prune old sessions

Session directories accumulate as sessions end. `captain memory prune` (also
run automatically, silently, and best-effort at every launch) removes
directories where nothing has been touched for `--older-than` days (7 by
default) and Herdr reports no live agent in their panes; when Herdr is
unreachable, only directories twice that age are removed. The current
session, the durable project graph, and the committed repo graph are never
removed.

```sh
captain memory prune
captain memory prune --older-than 30
```
