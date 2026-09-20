# Memory

Captain stores graph relationships and launch metadata outside the
repository, split so durable project facts survive OS temp cleanup while
ephemeral session state does not:

```text
~/.local/state/captain-barbossa/<hash of project path>/
  graph.json                      # explicit --scope project facts

<OS temp>/captain-barbossa-<uid>/<hash of project path>/
  sessions/<session-id>/
    session.json                  # workspace and crew references
    graph.json                    # this session's memory only
    events/<crew>.jsonl           # native hook events, plus cursor files
```

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
captain memory show
captain memory query "rate limiter"
captain memory path
```

`memory show` prints the most recent relationships as `[scope] [subject,
relation, object]`; `--all` shows every link, `--json` dumps the raw graph.
Default scope is `session`; use `--scope project` only for facts that should
survive into future sessions.

[Graphify](https://graphify.com/docs/cli) is optional: install it with
`uv tool install graphifyy` to enable `memory query`, which runs against an
isolated snapshot of project and session memory that is removed afterward.
Relationships can still be added and read with `memory add`/`show` without it.

## Prune old sessions

Session directories accumulate as sessions end. `captain memory prune` (also
run automatically, silently, and best-effort at every launch) removes
directories where nothing has been touched for `--older-than` days (7 by
default) and Herdr reports no live agent in their panes; when Herdr is
unreachable, only directories twice that age are removed. The current
session and the durable project graph are never removed.

```sh
captain memory prune
captain memory prune --older-than 30
```
