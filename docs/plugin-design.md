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

A repo's committed `.captain/settings.toml` may set `[plugins.<name>.options]` for a plugin already registered at home.
A project table for an unregistered plugin is never read for `command` and never executed.

### Protocol

One process per call: JSON request on stdin, JSON response on stdout, a deadline enforced by process-group kill (liveness, not security).

| Call | Purpose |
| --- | --- |
| `hello` | Plugin lists protocol versions, extension points, per-point versions and which points it requires |
| `sync` | Fetch a full server snapshot into a staging directory captain supplies |
| `call` | Invoke one extension point with a point-specific payload, for example a permission decision |

Every request carries `protocol`, `point`, `version`, `captain_version` and the plugin's merged options.

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
| Additive | Context, instruction append, memory and audit reads | A section per plugin, each with its own budget under a captain aggregate ceiling |
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
<state root>/plugins/<name>/<server id>/
  current.json    {"generation": "g-<time>-<hex>", "synced_at": ..., "protocol": 1}
  g-.../          immutable after publication
  refs/<holder>   one marker per pinned reader
  status.json  sync.lock
```

The server id includes the org identity, so switching organizations never reuses the previous context.

Sync takes `sync.lock` (skip if held), runs `sync` into staging, checks the envelope only (JSON manifest, supported protocol, matching name, declared files present), renames staging to `g-...` read-only, then atomically replaces `current.json`.
On failure it deletes staging, writes `status.json` and keeps the current generation.

- Replacement is wholesale.
  User changes go to the server through the plugin and return only in a later full snapshot.
- Launch and resume read the current generation and may start a detached refresh when `synced_at` exceeds the plugin's declared interval; they never block on the network.
- `recover` only reads: no sync, no directory creation.
- The org scheduler (launchd, systemd, cron) drives the interval by calling `captain plugin sync`, which also runs on demand.
- A crew pins its generation at recruit by creating a ref and holds it until dismissal.
- Collection runs under the sync lock and deletes a generation only if it is not current and has no refs.
  A reader whose generation vanished after creating its ref re-reads `current.json` and retries.
  Session prune releases the refs of sessions it removes.
- Old data is marked `stale` with `synced_at`; with no generation ever synced, nothing is injected and launch proceeds.

## 7. Versioning

| Version | Covers | Negotiated |
| --- | --- | --- |
| Transport | Envelope, calls, deadlines | `hello`, at `plugin add` and each session binding |
| Extension point | Payload semantics of one point | `hello`, per point |
| Session binding | Shape of the plugin record in the crew record | Read by hooks; unknown keys ignored |

- Captain keeps a supported-versions table per point and sends it with every request.
- An unknown optional point is skipped.
- An unsupported required point blocks activation of that plugin with one line naming the point and version.

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
| Crash or full disk mid-publish | Orphan staging and generations are collected; the pointer names only complete generations |

## 10. Phased plan

Phase 1 - registration, mirror, context.
Proposed files:

- `src/captain_barbossa/plugins.py`: registration reader, `hello` negotiation, call transport.
- `src/captain_barbossa/plugin_mirror.py`: staging, publication, pinned reads, collection.
- `cli.py`: `--no-plugins` and the `plugin` verb (`add`, `remove`, `list`, `sync`, `status`).
- Launch and `recovery.digest` adapters that add the context section only when a plugin is registered.
- `docs/plugins.md`; a pointer in `docs/settings.md`; `defaults.toml` unchanged, since `[plugins]` is a registry the user writes.

Tests, one file per behaviour:
- `tests/test_plugins_absent_are_inert.py`: output, prompts, argv, env, imports, I/O and subprocess calls equal today's; `hook` never imports plugins; a repo-only `[plugins.x]` is ignored.
- `tests/test_plugin_mirror_publication.py`: wholesale replacement, failure keeps current, lock contention, crash points, pinned-reader collection.
- `tests/test_plugin_context_delivery.py`: four providers, separate budget, escaped-byte limit, `recover` never syncs.
- `tests/test_plugin_protocol_negotiation.py`: optional skipped, required blocks, mixed versions.

Later:
1. Instructions, settings layer with provenance, session bindings; then namespaced commands and memory or audit pulls.
2. Permissions, `check` and `do` adapters, Claude first, with exclusive-owner resolution.
3. Recruitment decision, native launch, acknowledged outbox and remaining replace points.

## 11. Open questions

| Question | Recommended answer |
| --- | --- |
| Default refresh interval? | Plugin declares it, 15 minutes suggested; the org scheduler enforces it |
| Failure outcome for an exclusive policy call? | Plugin declares it; recommend refuse |
| Where do plugin defaults live? | In the plugin manifest; the one documented exception to defaults-in-`defaults.toml` |
| Which memory flows upload automatically? | None at first; explicit verbs, then scoped subscriptions with delete and supersede semantics |
| Support window after a transport major? | Current and previous major for two captain minor releases |
