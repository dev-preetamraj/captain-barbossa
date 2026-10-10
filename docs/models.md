# Models

Every provider the native CLIs support, which tier each one defaults to, and how
free text resolves to a model ID. `captain models [--agent claude|codex|pi|grok]`
prints this same information live, including any `[models.*]` overrides and, for
`pi`, whatever models this install has actually authenticated.

## How resolution works

A `--model`/`[models.*]` value is a tier (`cheap`, `mid`, `strong`), a model ID, or
an alias. Resolution tries, in order: an exact tier name, an exact ID or alias, a
real prefix match, a real substring match, then a close-spelling guess. A real
prefix or substring match naming more than one model is ambiguous and asks which
one you meant. A close-spelling guess that is ambiguous is not treated as a match
at all; it falls through to "no model matches", listing the valid IDs, since a
guess that is merely similar to several models was never really one of them.

A tier value that matches no known model (literal ID typo, or a model this release
does not yet know about) is kept as-is and passed to the CLI literally, so a model
newer than this release still works; see
[settings.md](https://github.com/dev-preetamraj/captain-barbossa/blob/main/docs/settings.md).

## claude

Four models, weakest to strongest. IDs and aliases, from the installed Claude
Code CLI's own model catalog:

| Tier (shipped default) | ID | Alias |
| --- | --- | --- |
| `cheap` | `claude-haiku-5-5` | `haiku` |
| `mid` | `claude-sonnet-5-5` | `sonnet` |
| `strong` | `claude-opus-5-5` | `opus` |
| (none) | `claude-fable-5-1` | `fable` |

## codex

Three generations, several of them sharing a name (`luna`/`terra`/`sol`). A bare
alias names only the current generation's model; an older generation is reachable
by its full ID. Weakest to strongest:

| Tier (shipped default) | ID | Alias |
| --- | --- | --- |
| `cheap` | `gpt-5.6-luna` | (none; superseded by `gpt-6-luna`) |
| (none) | `gpt-5.6-terra` | `terra` |
| `mid` | `gpt-5.6-sol` | (none; superseded by `gpt-6.1-sol`) |
| (none) | `gpt-6-luna` | `luna` |
| (none) | `gpt-6-sol` | (none; superseded by `gpt-6.1-sol`) |
| (none) | `gpt-6.1-sol` | `sol` |
| `strong` | `gpt-6-astra` | `astra` |

`gpt-5.5` is not listed: it retires 2026-10-14 and no longer appears in the
installed Codex CLI's model catalog.

## grok

Four models, weakest to strongest, matching `grok models`:

| Tier (shipped default) | ID | Alias |
| --- | --- | --- |
| `cheap` | `grok-4.7-build-fast` | `grok 4.7 build fast` |
| `mid` | `grok-4.6` | `grok 4.6` |
| `strong` | `grok-4.7` | `grok 4.7` |
| (none) | `grok-4.5` | `grok 4.5` |

Aliases are the labels Grok's own CLI shows and echoes back on a switch.

## pi

Not shipped: pi is provider-agnostic, so its catalog is whatever this install has
authenticated, read from `pi --list-models` and ranked by thinking support,
context window, then max output. Run `captain models --agent pi` for the live
list. A `[models.pi]` table in settings overrides its tiers the same way, using
pi's `provider/model` IDs.
