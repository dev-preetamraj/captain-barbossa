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

`defaults.toml` holds the shipped defaults and is their single source: the tier
table the code resolves against is read from it, and `captain init`
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

## What can be configured

The commented list of shipped keys and defaults is in
[`defaults.toml`](https://github.com/dev-preetamraj/captain-barbossa/blob/main/src/captain_barbossa/defaults.toml)
and is exactly what `captain init` writes. The tables cover:

- `[captain]`: the captain's CLI and optional starting model.
- `[crew]`: the default crew model and wait timeout.
- `[dashboard]`: automatic launch, refresh interval, pane ratio, context limit,
  and price-file override. See
  [dashboard.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/dashboard.md).
- `[placement]`: the captain-tab and crew-tab shapes. See
  [placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md).
- `[models.<provider>]`: the model behind each provider-neutral tier.

CLI flags still win over settings for one invocation. An empty `[captain] model`
passes no model flag, leaving the choice to the native CLI. A tier resolves through
the matching `[models.<provider>]` table, including overrides. `pi` has no fixed
catalog: its tiers are ranked from `pi --list-models`, and overrides use the
`provider/model` IDs pi prints.

Dashboard `prices_file` expands `~`. Changing `interval` needs no captain restart
because the dashboard reads it in its own process. A failure to open an enabled
dashboard prints `captain: no dashboard pane: ...` but does not stop the captain.

## Unknown and invalid values

| Situation | What happens |
| --- | --- |
| A tier value in `[models.<provider>]` that matches no known model | Kept literally and passed to that CLI as written, so a model newer than this release still works. |
| `[captain] model` that matches no known model | The launch fails, naming the options: `No claude model matches 'gigantic'. Tiers: cheap, mid, strong. Options: claude-haiku-4-5, claude-sonnet-5, claude-opus-5, claude-fable-5-1.` |
| `[captain] agent` outside `claude`, `codex`, `pi` | The launch fails naming the three, rather than trying to run it. |
| A value of the wrong type (`model = 123`, `enabled = "true"`, `interval = "5"`) | Ignored, and the setting falls back to the shipped default. A malformed `[placement]` shape is the one exception and fails loudly. Nothing is coerced across types: a string is never read as a boolean or a number. The one latitude is an int where a float is wanted, so `interval = 5` works. |
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
