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

The project root is the stable identity the rest of the CLI uses: `$CAPTAIN_PROJECT`
when a captain set it, otherwise the enclosing Git repository, otherwise the current
directory. Crew share that checkout, its project settings, session namespace, and
memory identity.

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

## Explicit recruitment profiles

Select a named preset with `captain crew --profile review --task "Review this change"`.
Profiles live in the same global and project settings files:

```toml
[profiles.review]
agent = "codex"
model = "mid"
execution = "foreground"
placement = "tab"

[profiles.research]
agent = "codex"
model = "cheap"
execution = "background"
```

All four keys are optional. `agent` accepts the usual providers, `model` accepts
a tier or model name, `execution` accepts `foreground` or `background`, and
`placement` accepts `auto`, `pane`, or `tab`.
Project keys override global keys
within the named profile. Explicit flags override that profile, then omitted
fields use existing recruitment defaults. Model tiers resolve for the effective
agent. `--foreground` overrides a background profile; `--background` overrides
foreground execution. Explicit split flags imply pane placement ahead of the
profile's placement.

Profiles are only selected by `--profile`; there is no implicit default profile.
Unknown names, unknown keys and invalid values fail before crew allocation.
Background execution still requires codex or pi, accepts only read/search grants,
and rejects placement, ownership and handoff inputs, including profile placement.
All crew share the captain checkout, including dirty, staged, and untracked files.
Profiles cannot contain `owns`, `allow`, tasks or any other assignment grants.

Profile recruits print and save `profile`, `effective`, and per-field `sources`
in their crew record. Effective values include the resolved model ID and actual
placement; sources identify flags, profile keys and settings files, or existing
defaults. Recruitment without `--profile` keeps its existing behavior.

## `captain init`

```sh
captain init            # writes <project>/.captain/settings.toml
captain init --global   # writes ~/.captain/settings.toml
```

`init` writes a template with every setting commented out at its current default, so
a freshly written file changes nothing until you uncomment a line. It creates the
`.captain` directory if needed and prints the path it wrote.

**It tops up, never overwrites.** If the file already exists, `init` appends only
the sections and keys the file never mentioned, commented out at their defaults,
and prints what it added. Every existing line stays byte for byte. A file that
already mentions every setting is left alone, and `init` says so.

`init` needs no Herdr pane, so it works from any shell.

## What can be configured

The commented list of shipped keys and defaults is in
[`defaults.toml`](https://github.com/dev-preetamraj/captain-barbossa/blob/main/src/captain_barbossa/defaults.toml)
and is exactly what `captain init` writes. The tables cover:

- `[captain]`: the captain's CLI and optional starting model.
- `[crew]`: default crew model, wait timeout, and background turn timeout.
- `[lifetime]`: managed-writer startup, termination and shutdown timeouts, plus polling interval.
- `[dashboard]`: automatic launch, refresh interval, pane ratio, context limit,
  and price-file override. See
  [dashboard.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/dashboard.md).
- `[placement]`: the captain-tab and crew-tab shapes. See
  [placement.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/placement.md).
- `[models.<provider>]`: the model behind each provider-neutral tier. See
  [models.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/models.md)
  for the full ID and alias reference, and `captain models` for what is installed now.

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
| `[captain] model` that matches no known model | The launch fails, naming the options: `No claude model matches 'gigantic'. Tiers: cheap, mid, strong. Options: claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5, claude-fable-5-1.` |
| `[captain] agent` outside `claude`, `codex`, `pi`, `grok` | The launch fails naming them, rather than trying to run it. |
| A value of the wrong type (`model = 123`, `enabled = "true"`, `interval = "5"`) | Ignored, and the setting falls back to the shipped default. Malformed `[placement]` shapes and selected profiles fail loudly. Nothing is coerced across types: a string is never read as a boolean or a number. The one latitude is an int where a float is wanted, so `interval = 5` works. |
| An unknown table or key | Ignored, except unknown keys in a selected profile, which fail. |

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
