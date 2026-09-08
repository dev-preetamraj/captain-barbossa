# Contributing to Captain Barbossa

Bug reports, documentation fixes, and focused pull requests are welcome. For a
larger change, open an issue first so we can agree on the intended behavior.
Be respectful, explain tradeoffs, and review the code rather than the person.

## Set up a checkout

Use macOS or Linux, Python 3.11 or newer, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Fork the repository on GitHub, then clone your fork:

```sh
git clone git@github.com:YOUR_USERNAME/captain-barbossa.git
cd captain-barbossa
git remote add upstream git@github.com:dev-preetamraj/captain-barbossa.git
uv sync --locked
uv run --locked pre-commit install
git switch -c fix/describe-the-change
```

`pre-commit install` installs **pre-commit, commit-msg, and pre-push hooks** in
this checkout. Repeat it after each fresh clone. The development tools are pinned
in `uv.lock`; they are not runtime dependencies of the installed `captain` tool.

Herdr, agent authentication, and Graphify are not required for the core tests.
Live CLI checks need an interactive Herdr workspace and a signed-in agent.

## Check your changes

```sh
# Apply formatting and fix supported lint issues.
uv run --locked ruff check --fix .
uv run --locked ruff format .

# Run the same complete gate used before every push.
uv run --locked pre-commit run --all-files --hook-stage pre-push
```

The gate checks formatting, lint, and the entire unittest suite. It fails the
push if any check fails; it does not silently rewrite files during a push.
Fix the problem, stage the corrected files, commit, and push again.

For a shorter feedback loop:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m captain_barbossa --help
```

Ruff owns Python formatting and import order; the line-length target is 100.
Add a focused regression test for changed behavior. Use temporary directories
outside the checkout for test state. Native Herdr mutations and model sessions
are mocked; the suite also exercises actual terminal input. The Graphify test
runs when `graphify` is installed and otherwise reports a skip.

CI repeats lint and format checks, validates commit messages and PR titles, and
runs tests on Linux and macOS with Python 3.11 and 3.14. It also builds the package
and checks that the wheel's CLI works outside the checkout. CI runs for pull
requests and pushes to `main`; local hooks must be installed to guard your pushes.

## Write a useful commit message

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```text
type(optional-scope): concise description
```

Examples:

```text
fix(crew): wait for native agent readiness
feat(memory): add a session export command
docs: clarify contributor setup
ci: test supported Python versions
```

Use `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, or `revert`. Keep the first line at most 72 characters. Use a body to
explain why a change is needed. Mark incompatible changes with `!`, such as
`feat(cli)!: change the session argument`, and explain the migration in the body.

The commit-msg hook validates messages with Commitizen. `uv run --locked cz commit`
provides an interactive helper. Git-generated merge/revert messages and temporary
fixup/squash commits use Commitizen's standard exceptions. Use a conventional PR
title too, so a squash merge produces a useful commit message. The original
initial commit predates this policy and does not need rewriting.

## Open a pull request

Keep the change focused, describe the problem and resulting behavior, and include
the commands you ran with their results. Update documentation when behavior or
setup changes. For terminal changes, state which agent, Herdr version, and OS you
tested; distinguish native checks from mocks. The PR template provides a short
checklist. Maintainers should merge only after the CI checks pass.

Keep runtime state outside the repository, preserve native agent permissions,
and ask the user for missing agent and pane/tab choices. Follow the current
[plan](docs/plan.md).

When changing dependencies, use `uv add` (or `uv add --dev`) and commit both
`pyproject.toml` and `uv.lock`. Validate packaging changes with `uv build`.
Do not commit virtual environments, caches, credentials, or private transcripts.

## License

Captain Barbossa is licensed under [MIT](LICENSE). Contributions are made under
the same license. Only contribute material you have the right to share, and
preserve applicable third-party notices.
