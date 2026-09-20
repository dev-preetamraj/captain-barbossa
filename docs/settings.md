# Settings

Captain Barbossa reads optional user settings from `.captain/settings.toml`. There
is no required configuration: with no file anywhere, every setting falls back to the
built-in default and nothing warns about it.

## Where the files live

| File | Scope |
| --- | --- |
| `<project>/.captain/settings.toml` | This project only. Commit it to share the settings with the repo, or ignore it to keep them yours. |
| `~/.captain/settings.toml` | Every project on this machine. |
| `captain_barbossa/defaults.toml` | The shipped defaults, inside the installed package. The bottom layer, not meant to be edited. |

`defaults.toml` holds every default this page lists, and is the single source for
them: the tier table the code resolves against is read from it, and `captain init`
writes it back out with the values commented, so the defaults, the code, and the
template cannot drift apart. Editing the installed copy is not the way to change a
setting (an upgrade replaces it): write the key in one of the two files above.

The project root is the same one the rest of the CLI uses: `$CAPTAIN_PROJECT` when a
captain set it, otherwise the enclosing git repository, otherwise the current
directory.

## Precedence

Settings resolve **per key**, not per file or per table:

```text
project key  >  global key  >  defaults.toml
```

A file that sets one key overrides only that key. Everything it leaves out keeps
falling back, so these three files:

```toml
# ~/.captain/settings.toml
[captain]
model = "strong"

[models.claude]
cheap = "sonnet"
```

```toml
# <project>/.captain/settings.toml
[models.claude]
cheap = "fable"
```

resolve to `[captain] model = "strong"` (only the global file sets it),
`[models.claude] cheap = "claude-fable-5-1"` (the project file wins), and
`mid`/`strong` still on their built-in defaults (neither file mentions them).

## `captain init`

```sh
captain init            # writes <project>/.captain/settings.toml
captain init --global   # writes ~/.captain/settings.toml
```

`init` writes a template with every setting commented out at its current default, so
a freshly written file changes nothing until you uncomment a line. It creates the
`.captain` directory if needed and prints the path it wrote.

**It never overwrites.** If the file already exists, `init` prints its path, leaves
the contents exactly as they are, and exits 0. To start over, delete the file first.

`init` needs no Herdr pane, so it works from any shell.

## `[captain]`

Settings for the captain's own session, applied when `captain` launches its native
CLI.

| Key | Default | Value |
| --- | --- | --- |
| `model` | unset | A tier (`cheap`, `mid`, `strong`), a model ID, or an alias. |

```toml
[captain]
model = "strong"
```

Left unset, the captain's CLI starts on whatever model that CLI defaults to;
Captain Barbossa passes no model flag at all. A tier here resolves through the same
table crew use, including any `[models.<provider>]` override below, so
`model = "cheap"` follows your configured `cheap` rather than the built-in one.

The value is resolved against the provider the captain actually launches with, so
one setting works across `--agent claude|codex|pi`.

## `[dashboard]`

The crew token-usage pane that a captain can open below itself at launch.

| Key | Default | Value |
| --- | --- | --- |
| `enabled` | `false` | `true` to open the pane when `captain` launches. |

```toml
[dashboard]
enabled = true
```

The pane is opt-in: by default `captain` launches without it, and you run one by
hand in any pane with `captain dashboard`. `captain --no-dashboard` skips the pane
even when this is `true`, so a single launch can opt out without editing the file.

A failure to open the pane never costs the captain its launch: it prints
`captain: no dashboard pane: ...` and carries on.

This key is a real TOML boolean. `enabled = "true"` is a string, not a boolean, so it
is ignored and the pane stays shut.

## `[models.<provider>]`

Retier a provider: which model each of the three provider-neutral tiers means for
that CLI. One table per provider, and each key is independent.

| Key | `claude` default | `codex` default | `pi` default |
| --- | --- | --- | --- |
| `cheap` | `claude-haiku-4-5` | `gpt-5.6-luna` | weakest model pi reports |
| `mid` | `claude-sonnet-5` | `gpt-5.6-sol` | middle model pi reports |
| `strong` | `claude-opus-5` | `gpt-6-astra` | strongest model pi reports |

```toml
[models.claude]
cheap = "sonnet"       # only cheap moves; mid and strong keep their defaults

[models.codex]
strong = "gpt-5.5"

[models.pi]
cheap = "ollama/llama3.2:3b"
```

Values take a model ID or an alias (`sonnet`, `opus`, `astra`, `terra`). Aliases are
matched loosely, so case and spacing do not matter.

`pi` has no fixed catalog: its tiers are ranked from `pi --list-models`, so the
defaults follow whatever that install has authenticated, and IDs are the
`provider/model` form pi itself prints.

These overrides apply everywhere a tier is used: `captain crew --model cheap`,
`captain model <name> strong`, `[captain] model`, and the `cheap` default that crew
get when `--model` is omitted.

## Unknown and invalid values

| Situation | What happens |
| --- | --- |
| A tier value in `[models.<provider>]` that matches no known model | Kept literally and passed to that CLI as written, so a model newer than this release still works. |
| `[captain] model` that matches no known model | The launch fails, naming the options: `No claude model matches 'gigantic'. Tiers: cheap, mid, strong. Options: claude-haiku-4-5, claude-sonnet-5, claude-opus-5, claude-fable-5-1.` |
| A value of the wrong type (`model = 123`, `enabled = "true"`) | Ignored, and the setting falls back as if unset. Strings are never coerced to booleans. |
| An unknown table or key | Ignored. |

The difference between the first two rows is deliberate: a `[models.*]` tier is a
pass-through to the CLI, while `[captain] model` is resolved up front so a typo
fails loudly at launch instead of silently starting the wrong model.

## Malformed files

A file that is not valid TOML is an error naming the file, and no command runs with
half-read settings:

```text
captain: Could not read settings from /path/to/.captain/settings.toml: Expected ']'
at the end of a table declaration (at line 1, column 9)
```

An unreadable file (permissions, a directory in its place) reports the same way. A
file that simply does not exist is not an error: that is the normal case.

## Full example

Everything currently supported, in one file:

```toml
[captain]
model = "strong"

[dashboard]
enabled = true

[models.claude]
cheap = "claude-haiku-4-5"
mid = "claude-sonnet-5"
strong = "claude-opus-5"

[models.codex]
cheap = "gpt-5.6-luna"
mid = "gpt-5.6-sol"
strong = "gpt-6-astra"

[models.pi]
cheap = "ollama/llama3.2:3b"
mid = "anthropic/claude-sonnet-5"
strong = "openai-codex/gpt-6-astra"
```
