# Plugin system: technical design

Design only. No implementation.
Covers how an organization extends captain-barbossa through an external plugin, and what captain itself must never do for it.
Firstmate is the example org plugin throughout.

## 1. Problem and goals

An organization wants captain to carry its knowledge, policy and workflow without a fork.
Today the levers are settings, the repo graph and generated instructions, all local and static.

- An org installs one external executable and captain changes behaviour at a published list of extension points.
- Org context reaches the captain and every crew without competing for the 32,000-character recovery cut.
- Users with no plugin see no change in behaviour, tokens, latency, I/O or dependencies.
- Several plugins coexist, and plugins and captain upgrade independently.

## 2. Non-goals

- Plugin security: plugins are org-owned, so every risk is the plugin's.
  Captain has no sandbox, content check, rules language, capability system or tighten-only merge.
- Python entry points or in-process plugin code.
- A captain daemon; scheduling belongs to the org.
- Letting a repo install or activate a plugin.
- Merging server data into captain memory.
- Changing the mail protocol, assignment lifecycle or Codex hook strings.

## 3. Architecture

A plugin is an executable speaking a versioned JSON protocol.
Captain owns transport, negotiation, mirror publication and adapters; the plugin owns what its org means.

Install is registration in `~/.captain/settings.toml`, written by `captain plugin add` or the org installer.
Installed means active: no launcher, wrapper or enrollment step.
`--no-plugins` disables every plugin for one run.

Only `[plugins.<name>]` tables in home settings are read.
A repo's committed `.captain/settings.toml` `[plugins]` table is ignored whole, options included, so a repo can neither activate a plugin nor change what one is told.
Repo-level options for a home-registered plugin are deferred, not excluded.

### Protocol

One process per call: JSON request on stdin, JSON response on stdout, a deadline enforced by process-group kill (liveness, not security).

| Call | Purpose |
| --- | --- |
| `hello` | Plugin lists protocol versions, extension points, per-point versions and which points it requires |
| `sync` | Return a full server snapshot inline; captain writes and publishes it |
| `call` | Invoke one extension point with a point-specific payload, for example a permission decision |

Every request carries `protocol`, `call`, `captain_version` and the plugin's options; a `call` request adds `point` and `version`.
The phase 1 wire contract, including golden envelopes, is in section 10.

### Impact on users without a plugin

- No `[plugins]` table in home settings means the plugin module is never imported.
- The registration check is a lookup in home settings captain already loads: no extra file read, subprocess, import or dependency.
- Hooks never read home settings.
  They act on a plugin binding in the crew record, which a crew recruited without plugins does not have.
- The only visible change is help text: the `--no-plugins` flag and the `captain plugin` management verb now appear.

## 4. Extension points

Append adds to what captain produces; replace substitutes for it.
Points not built in the first increment are deferred, not excluded.

| Point | Append | Replace | Notes |
| --- | --- | --- | --- |
| Context | Per-plugin section after the recovery digest | - | Outside the 32,000-char cut, own byte budget |
| Instructions | Per-plugin text for captain, crew, background and quiet | Whole generated role text | Replace is exclusive |
| Settings, models, recruitment | Settings layer from the mirror | Recruitment decision (provider, model, placement) | Decision is exclusive |
| Crew command denial | Plugin-declared commands crew may never run | - | Additive; phase 1 |
| Permissions, `check`, `do` | - | Org policy overrides captain's defaults | Exclusive |
| Native launch | Argv and environment additions per provider | Provider adapter | Chained |
| Memory and audit | Pull from state-root paths at sync; lifecycle events fan out | - | Explicit verbs first, acknowledged outbox later |
| CLI commands | `captain plugin run NAME ...` | - | Namespaced; refused for crew |

Permissions limits captain does not hide:

- Only Claude crew pass the per-tool gate, and `check` is cooperative, not equivalent enforcement.
- Captain hands the plugin every hook request, including unknown tools, and maps its verdict onto `approval.Decision` through an adapter.
- Firstmate must refuse providers and configurations it cannot enforce.

## 5. Multiple plugins

Order is the order of `[plugins.<name>]` tables in `~/.captain/settings.toml`.

| Kind | Points | Rule |
| --- | --- | --- |
| Additive | Context, crew command denial, instruction append, memory and audit reads | A section per plugin, each with its own budget under a captain aggregate ceiling; denials are the union |
| Chained | Settings, native launch | Declared order, each seeing the previous output; home, project and flags still win |
| Exclusive | Permissions, instruction replace, recruitment decision | One owner |
| Namespaced | CLI commands | `captain plugin run NAME`; names never collide |
| Fan-out | Lifecycle events | Every subscribed plugin receives each event |
| Per-plugin | Mirror, sync, failures | A failing plugin is skipped and reported; it never blocks another |

Settings read bottom to top: `defaults.toml`, plugin layers in declared order, home, project, flags.
`config.source()` names the plugin layer from the same generation that produced the value.

Two plugins claiming one exclusive point disables both for it until the user names one, for example `[plugins] permissions = "firstmate"`.
Only home settings may name the owner, since a repo choosing one would be activating a plugin.
Until named, captain uses its own default for that point and prints one line saying why.

## 6. Sync and mirror

The mirror is a read-only cache of server context.
It is never edited, annotated or merged into captain memory.

```text
<state root>/plugins/<name>/
  current.json    {"generation": "<ns time>-<hex>", "server_id": ..., "synced_at": ..., "expires_at": ...}
  hello.json      last successful hello response, with the negotiated versions
  generations/<id>/   manifest.json and files, written by captain, immutable after publication
  refs/<id>@<sha256(session dir)[:16]>   one per generation a session's crew pinned; holds the session dir
  status.json  lock  .tmp-<hex>/
```

The mirror is per user, not per project: one sync serves every project, and the org scheduler needs no project to call it.

`server_id` is opaque and plugin-supplied, returned by each successful `sync`.
Captain stores and shows it, and passes the last one back on the next `sync`; it never parses it or adds an org or tenant id.
The plugin must return a different `server_id` for every data boundary it serves; each sync replaces the whole current generation, so a change of `server_id` never mixes boundaries.
Firstmate returns its API host, which is safe only while no host serves more than one organization; that is the plugin's deployment constraint, not captain's.

Sync takes `lock` (exit with one line if held), runs `hello`, then `sync`, which returns the whole snapshot inline in its response.
Captain checks the envelope only (shape, paths, the context rules of section 10), writes the files into `.tmp-<hex>/`, renames it into `generations/`, then atomically replaces `current.json`.
Captain assigns the generation id; the plugin never names a directory.
On failure it deletes the temporary directory, writes `status.json` and keeps the current generation.
A successful response with `files: []` is a valid generation and becomes current; it injects nothing, which is how a plugin wipes the mirror.

- Replacement is wholesale.
  User changes go to the server through the plugin and return only in a later full snapshot.
- Launch reads the current generation and starts a detached `captain plugin sync <name>` when `synced_at` is older than the plugin's `refresh_interval` from `hello` (900 seconds when absent); it never runs `hello` or blocks on the network.
- `recover` only reads: no sync, no subprocess, no directory creation.
- The org scheduler (launchd, systemd, cron) drives the interval by calling `captain plugin sync`, which also runs on demand.
- A crew pins at recruit: its crew record names the generation, and a ref ties that generation to the session directory.
- Collection runs under the lock after each publish: it drops refs whose session directory no longer exists, then deletes generations that are neither current nor referenced.
  Pins are released by session prune, never by dismissal, so a generation lives as long as the session that used it and there is no release path to get wrong.
- A generation whose `synced_at` is over 24 hours old is injected with a freshness line that says it is stale and when it was synced.
- At or after the manifest's `expires_at`, nothing from that generation is injected, stale or not, and `status` says expired.
- With no generation ever synced, nothing is injected and launch proceeds.

## 7. Versioning

| Version | Covers | Negotiated |
| --- | --- | --- |
| Transport | Envelope, calls, deadlines | `hello`, at `plugin add` and each sync |
| Extension point | Payload semantics of one point | `hello`, per point |
| Session binding | Shape of the plugin record in the crew record | Read by hooks; unknown keys ignored |

- Captain keeps a supported-versions table per point and sends it with every request.
- For each point the plugin lists in `hello`, captain picks the highest version both support.
- A point captain does not support at a common version is skipped, unless the plugin marks it required.
- A transport mismatch, or a required point with no common version, is a version mismatch.
- On a version mismatch an optional plugin is skipped with one stderr line, and a plugin registered with `required = true` blocks launch with that line.
- Launch re-negotiates from the cached `hello.json`, so a captain upgrade is noticed without a subprocess.

Assigned in phase 1:

| Version | Value | Meaning |
| --- | --- | --- |
| Transport | 1 | The envelopes in section 10 |
| `context` point | 1 | Manifest with required `expires_at`; there is no expiry-less context version, so no plugin can activate against one |
| `crew_denials` point | 1 | Commands declared in `hello`, enforced as in section 10 |
| Session binding | 1 | `plugins` in session meta (denials and active plugins, fixed at launch) and in each crew record (pinned generations and denials, fixed at recruit) |

## 8. Responsibility split

| Captain | Org plugin |
| --- | --- |
| Registration, `--no-plugins`, `captain plugin` verb | Installer and what it registers |
| Transport, negotiation, deadlines, process-group kill | Executable, server protocol, authentication |
| Mirror publication, pointer swap, pinning, collection | Snapshot content, schema inside a generation, refresh interval |
| Hard ceilings on bytes added to launch | What the text says and how it fits its budget |
| Adapters onto settings, instructions, approval, launch | Policy, decisions, composition inside its scope |
| Exclusive-owner conflict detection | Provider enforcement and refusal |

Captain does not inspect, scan, restrict or vouch for plugin output.

## 9. Edge cases

| Case | Behaviour |
| --- | --- |
| Plugin missing, failing, offline or hung | `status.json` records it, the current generation keeps serving as `stale`, calls hit the deadline and the point's declared failure outcome applies |
| Plugin removed or `--no-plugins` | New launches ignore it; live crew keep their pinned binding until dismissed, and delivered prompt text cannot be revoked |
| Captain upgraded, plugin older | Works while a transport major is shared; otherwise inactive with one line |
| External decision inside the events lock | Run outside the lock and revalidate the grant after |
| Reentrancy: plugin calls captain during a call made under `crew.lock` (recruitment) | Deadlocks; captain makes plugin calls outside `crew.lock` and revalidates after, and plugins must not call back into captain during a call |
| Crash or full disk mid-publish | Orphan temporary directories and generations are collected; the pointer names only complete generations |
| Manifest past `expires_at` | Nothing injected; `status` says expired; a pinned crew past expiry also gets nothing on recover |
| Generation with `files: []` | Becomes current; nothing injected; older unpinned generations are collected |
| `sync` returns a new `server_id` | The new generation becomes current; earlier generations are collected once unpinned |
| Context section over its ceiling or with forbidden bytes | Sync fails with `context_too_large` or `invalid_context`; the current generation keeps serving |
| Crew on a provider or path with no enforcing gate while a denial is active | Recruitment refused with one line naming the plugin and provider |

## 10. Phased plan

### Phase 1 - registration, mirror, context, crew command denial

Crew command denial is pulled forward from the permissions phase because the first consumer cannot enable shared-rule approval without it.
It is not the permissions point: no plugin is called at hook time, nothing is replaced, and captain's own approval table is unchanged.
Only the session binding that carries it is pulled forward from Later 1, at version 1.

#### Commands

- `captain plugin add NAME COMMAND [--required]`: resolve `COMMAND` to an absolute executable, run `hello`, refuse with one line on a version mismatch, else append `[plugins.NAME]` to home settings and run a first sync.
- `captain plugin remove NAME`: delete that table; live crew keep their binding until their session is pruned.
- `captain plugin list`: registered plugins, negotiated versions, denials, active or skipped with the reason.
- `captain plugin sync [NAME]`: synchronous sync of one or every plugin; also the detached refresh and the org scheduler's entry point.
- `captain plugin status [NAME]`: `server_id`, current generation, `synced_at`, stale or expired, last failure code, rendered bytes against the ceiling.
- `--no-plugins` is a captain launch flag only.
  The session records it, so the captain model cannot pass it at recruit to drop a denial.

`plugin add` and `remove` edit home settings as text, because the repo has no TOML writer and one table does not justify a dependency.
The block is appended with JSON-quoted strings, which are valid TOML; removal drops the lines from the header to the next table.
Either edit is re-parsed and must equal the old settings plus or minus exactly that table, else captain refuses and says to edit by hand.

```toml
[plugins.example]
command = "/usr/local/bin/example"
required = false
```

Names match `[a-z][a-z0-9-]{0,31}`.
The `plugin` verb is captain-only like every non-protocol verb.

#### Transport

One process per call: `COMMAND` with no arguments, request JSON on stdin, response JSON on stdout, stderr discarded.
Deadlines: `hello` 10 seconds, `sync` 120 seconds, enforced by killing the process group; stdout over 4 MiB is a failure.
A nonzero exit, invalid JSON, an oversized or missed response is a failure with code `plugin_exit`, `bad_response` or `timeout`.
Captain sends no `call` in phase 1; a plugin answers any request it does not know with `unsupported_call`.

Golden envelopes live in `docs/plugin-protocol/`, and tests compare captain's requests to them and parse the responses from them.

`hello` request and response:

```json
{"protocol": 1, "call": "hello", "captain_version": "0.50.0", "options": {},
 "supported": {"protocol": [1], "points": {"context": [1], "crew_denials": [1]}}}
```

```json
{"ok": true, "name": "example", "protocol": [1], "refresh_interval": 900,
 "points": {"context": {"versions": [1], "required": true},
            "crew_denials": {"versions": [1], "required": true,
                             "commands": [["example", "proposals", "approve"]]}}}
```

`sync` request and response:

```json
{"protocol": 1, "call": "sync", "captain_version": "0.50.0", "options": {},
 "points": {"context": 1}, "server_id": "api.example.com"}
```

```json
{"ok": true, "server_id": "api.example.com", "expires_at": "2026-12-23T10:00:00Z",
 "files": [{"path": "context/captain.md", "roles": ["captain"], "text": "..."},
           {"path": "context/crew.md", "roles": ["crew"], "text": "..."}]}
```

Error, for any call:

```json
{"ok": false, "error": {"code": "login_required", "message": "one line, no secrets"}}
```

- `options` is the `options` table of the home registration, `{}` when absent.
- `server_id` in the request is the last one published, or `null`; in the response it is a non-empty string of at most 256 bytes.
- `expires_at` is required, RFC 3339 with an explicit offset.
- `path` is relative, `/`-separated, without `.` or `..` parts, unique; `roles` is a subset of `captain` and `crew`; `files` may be `[]`.
- Error codes are plugin-owned strings matching `[a-z_]{1,64}`; captain records the code in `status.json` and never puts the message into a prompt.
- Unknown keys are ignored in both directions.

#### Context injection

Context point 1 adds one section per plugin after the launch state, outside the 32,000-character cut, in registration order.
The captain gets files whose `roles` include `captain`; foreground and background crew get those that include `crew`; `quiet` gets none.

```text
## Plugin context: <name>
Synced <YYYY-MM-DDTHH:MM:SSZ> (fresh).

### <path>
<text>
```

The freshness line reads `(stale)` instead once the generation is over 24 hours old; it has a fixed width, so the size admitted at sync is the size injected later.

The byte contract, which a plugin uses for admission:

- Count `len(section.encode("utf-8"))` of the exact text appended, heading, freshness line, file headings and newlines included, before any escaping a provider applies.
- Per-plugin ceiling: 8,192 bytes for each role's section.
- Aggregate ceiling: 16,384 bytes of plugin sections per launch.
- `text` must contain no control characters other than LF and TAB, so provider escaping at most doubles it.
- Worst case for the one provider that escapes (codex): about 12,000 bytes of instructions, 64,000 for the escaped digest and 32,768 for escaped plugin context, which stays under Linux's 131,072-byte limit on one argument; the token cost is about 4,000 tokens per agent.
- A section over its ceiling fails sync with `context_too_large`, and a forbidden byte with `invalid_context`; the current generation keeps serving.
- Past the aggregate ceiling, later plugins are omitted whole and replaced by one line naming the plugin and the byte counts; nothing is ever truncated.

Raising a ceiling later is compatible, since a section admitted under a smaller one still fits.

#### Crew command denial

A plugin declares `commands`, each an argv prefix whose first word is a program name.
`hello.json` keeps them, so a skipped optional plugin's denials still apply; only `--no-plugins` and `plugin remove` drop them.
At launch captain copies every registered plugin's denials into session meta, and at recruit into the crew record, so hooks read only the crew record, never home settings or the plugin.
The check runs before assignment grants, so no grant can allow a denied command.

A tool request is checked when its input has a `command` string or list, whatever the tool's name.
Its text is split on whitespace, quotes and shell punctuation, and each word is reduced to its basename.
The request is denied when a word equals a declared program name, or that name with a `.py` or other extension, and the remaining words of that command follow it in order anywhere later in the text.
That deliberately over-denies, for example `echo "example proposals approve"`, and catches paths, `env`, `uv run`, `uvx`, `xargs`, `sh -c`, `bash -lc`, quoting, pipes, `&&`, `$(...)`, `python -m` and `a=example; $a proposals approve`.
It cannot see a command assembled from encoded text or run from a script file the crew wrote; the plugin's server remains the authority for who may act.

Where a provider or path has no verified gate, captain refuses rather than hoping:

| Launch path | claude | codex | pi | grok |
| --- | --- | --- | --- | --- |
| Foreground crew | PreToolUse deny | refused | crew extension `tool_call` block | refused |
| Background crew | already refused | refused | `--tools read`, no shell tool | already refused |
| `quiet` | no Bash tool | refused | no shell tool | `--deny Bash` |
| Captain | not denied: the human is in the loop | | | |

Codex has a PreToolUse hook, but an untrusted hook does not run and captain must never write Codex's trust store or bypass hook trust, so a denial there would fail open.
Grok's hooks need configuration files captain does not write.
Each refusal is one line naming the plugin, the provider and the path; with no denial active every path is unchanged.
A pi crew without the managed-session extension is refused for the same reason.

Every cell is a test row: the denied command and each alias above are refused or have no shell tool, and an unrelated command still reaches the existing gate unchanged.

#### Files

- `src/captain_barbossa/plugins.py`: registration, transport, negotiation, mirror, rendering and the denial matcher; it is imported only when a plugin is registered or a binding exists.
- `config.py`: a cached `home_settings()` that `settings()` reuses, so the registry is read from home only and costs no extra read.
- `cli.py`: `--no-plugins`, the `plugin` verb and the `recover` key.
- `agents.py`, `background.py`, `do.py`, `events.py`, `approval.py`, `pi_history.py`: the injection, pinning, refusal and gate adapters, each a no-op without a binding.
- `docs/plugins.md`, `docs/plugin-protocol/*.json`; a pointer in `docs/settings.md`; `defaults.toml` unchanged, since `[plugins]` is a registry the user writes and the contract numbers are not settings.

Tests, one file per behaviour, written first:

- `tests/test_plugins_absent_are_inert.py`: every existing command's output, prompts, argv, env, imports and subprocess calls equal today's; `hook` never imports plugins; a repo-only `[plugins.x]` is ignored.
  `--help` is the one exception, since it lists the new verb and flag.
- `tests/test_plugin_mirror_publication.py`: wholesale replacement, failure keeps current, lock contention, crash leftovers, pinned collection, empty generation, `server_id` change.
- `tests/test_plugin_context_delivery.py`: four providers, separate budget, byte contract, stale heading, expiry, detached refresh when due, `recover` never syncs.
- `tests/test_plugin_protocol_negotiation.py`: optional skipped, required blocks, mixed versions, golden envelopes.
- `tests/test_crew_command_denial.py`: the matrix above.

Later:
1. Instructions, settings layer with provenance, repo-level options, Codex and grok denial gates; then namespaced commands and memory or audit pulls.
2. Permissions, `check` and `do` adapters, Claude first, with exclusive-owner resolution.
3. Recruitment decision, native launch, acknowledged outbox and remaining replace points.

## 11. Open questions

| Question | Answer |
| --- | --- |
| Default refresh interval? | Decided: `refresh_interval` in `hello`, 900 seconds when absent; launch refresh is opportunistic and only the org scheduler guarantees periodic delivery |
| Failure outcome for an exclusive policy call? | Deferred with permissions; recommend refuse. Phase 1 denial makes no plugin call, so it has no failure outcome |
| Where do plugin defaults live? | Decided: in the plugin itself; captain passes only the home `options` table |
| Which memory flows upload automatically? | Decided for phase 1: none |
| Support window after a transport major? | Decided: current and previous major for two captain minor releases; phase 1 has only transport 1 |
| How do a plugin's own direct verbs get the same options? | Open, plugin-owned for phase 1: captain adds no options export; a later `captain plugin options NAME` is the candidate |
