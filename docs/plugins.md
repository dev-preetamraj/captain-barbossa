# Plugins

A plugin is an executable your organization installs so captain carries its context and policy without a fork.
It adds a context section to every captain and crew launch, and it can forbid crew a list of commands.
With no plugin registered, captain behaves exactly as it does without this feature.
The design and its reasoning are in [plugin-design.md](plugin-design.md); this page is the user and author guide.

## Using a plugin

Register an executable once:

```sh
captain plugin add example /usr/local/bin/example
```

`add` runs the plugin's `hello`, refuses with one line when captain and the plugin share no version, appends a table to `~/.captain/settings.toml`, then runs a first sync.
Pass `--required` when a launch must not proceed if this plugin cannot activate.

```toml
[plugins.example]
command = "/usr/local/bin/example"
required = false
```

Only this home file registers plugins.
A repository's `.captain/settings.toml` `[plugins]` table is ignored whole, so cloning a repo can never activate one.
An `options` table under `[plugins.example]` is passed to the plugin unchanged.
A malformed entry (a bad name, no `command` string, a value of the wrong type) is ignored silently, like any other wrong-typed setting; a non-boolean `required` counts as `false`.
A file that is not valid TOML still fails every command, as described in [settings.md](settings.md#malformed-files).

| Command | What it does |
| --- | --- |
| `captain plugin list` | Registered plugins, active or skipped, and their negotiated versions |
| `captain plugin sync [NAME]` | Refresh one or every mirror now |
| `captain plugin status [NAME]` | JSON: generation, `server_id`, freshness, expiry, last failure, bytes per role, denials |
| `captain plugin remove NAME` | Unregister; crew already running keep what they pinned |
| `captain --no-plugins` | Launch with no plugin context and no plugin denials for this whole session |

Launch never waits on a plugin.
It injects what the last successful sync published and starts a detached `captain plugin sync` when that sync is older than the plugin's refresh interval (900 seconds unless the plugin says otherwise).
For a guaranteed interval, schedule `captain plugin sync` with launchd, systemd or cron.
`captain recover` reads the mirror and never syncs.

## What a launch receives

The captain gets each active plugin's files marked for the `captain` role; foreground and background crew get those marked `crew`; `captain quiet` gets none.
Sections come after the launch state, outside its 32,000-character cut, in registration order:

```text
## Plugin context: example
Synced 2026-11-23T10:00:00Z (fresh).

### context/captain.md
<file text>
```

The freshness line says `(stale)` once the generation is over 24 hours old.
At or after the snapshot's `expires_at`, nothing from it is injected.
A crew pins the generation it was recruited with, so a later sync never changes what a running crew was told.

## Byte budget

A plugin uses this rule to admit its own content:

- Bytes are `len(section.encode("utf-8"))` of the exact section above: heading, freshness line, file headings and newlines included, before any escaping a provider applies.
- Each role's section is at most 8,192 bytes per plugin.
- All plugin sections in one launch total at most 16,384 bytes.
- File text may contain no control character other than LF and TAB.

A sync whose section is too large fails with `context_too_large`, and one with a forbidden character with `invalid_context`; the previous generation keeps serving.
When several plugins pass the total, later ones are omitted whole and replaced by one line naming the plugin; nothing is truncated.

## Crew command denial

A plugin may declare commands crew must never run, such as `["example", "proposals", "approve"]`.
A crew's shell request is refused when a declared program name appears in it, by basename, ignoring case, quoting and any extension, or as a glob such as `exampl?` that matches it, followed later by the rest of that command's words in order.
That covers full paths, `env`, `uv run`, `uvx`, `xargs`, `sh -c`, quoting, pipes, `$(...)` and `python -m module` when the module is declared as its own command.
It over-refuses on purpose, for example `echo "example proposals approve"`, and it cannot see commands built from variables or encoded text, or run from a script file; the plugin's server stays the authority.

| Launch path | claude | codex | pi | grok |
| --- | --- | --- | --- | --- |
| Foreground crew | PreToolUse hook denies | refused | crew extension blocks | refused |
| Background crew | not offered | refused | no shell tool | not offered |
| `quiet` | no shell tool | refused | no shell tool, extensions off | shell denied |

Codex's hooks run only after the user trusts them, which captain never does on their behalf, so a denial there could silently not apply.
While any denial is active, captain refuses those paths with one line rather than launching crew it cannot hold to it.
The captain itself is never denied.

## Writing a plugin

Captain runs the executable with no arguments, writes one JSON request to stdin and reads one JSON response from stdout.
`hello` has 10 seconds and `sync` 120; past that the process group is killed.
Stdout over 4 MiB, a nonzero exit or non-JSON output is a failure.
The exact envelopes are in [plugin-protocol/](plugin-protocol/), and captain's tests check its own requests and parsing against those files.

`hello` lists the transport versions and, per extension point, the versions the plugin speaks and whether it requires that point:

| Point | Version | Meaning |
| --- | --- | --- |
| `context` | 1 | `sync` returns files, roles and a required `expires_at` |
| `crew_denials` | 1 | `commands`, each a list of words starting with a program name |

Captain picks the highest common version of each point.
A required point or a transport with no common version skips the plugin with one line, or blocks launch when the user registered it with `--required`; its denials still apply either way.

`sync` returns the whole snapshot inline.
Captain validates it, writes it as a new generation and swaps one pointer, so a reader sees one complete snapshot or the previous one.
Return `files: []` to wipe what captain holds; that is a success, and it becomes current.
`server_id` is yours and opaque: captain stores it, shows it, and sends the last one back on the next `sync`.
Return a different one for each data boundary you serve.

Answer any request you do not support with the error envelope, for example code `unsupported_call`.
Error codes match `[a-z_]{1,64}`; captain records the code in `status` and never puts your message into a prompt.
Never call captain from inside a protocol call.
