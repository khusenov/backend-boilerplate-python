# Contributing

## Setup

You need [uv](https://docs.astral.sh/uv/) and Docker. uv installs Python 3.14 if you don't have it.

1. Clone the repository and run `uv sync`.
2. Start Docker, then run `make check` to confirm everything passes. The tests start their own PostgreSQL container.
3. Run `make dev` to start the API and the worker. [Development](docs/development.md) covers the local services and workflows.

## What `make check` enforces

`make check` changes nothing. It runs these commands in order and stops at the first failure:

| Command | Fails when |
|---|---|
| `uv lock --check` | `uv.lock` is out of date with `pyproject.toml` |
| `uv run ruff format --check` | A file isn't formatted |
| `uv run ruff check` | A lint rule or a banned import is hit |
| `uv run basedpyright` | A type error or warning is reported |
| `uv run lint-imports --no-logo` | A module imports across a forbidden boundary |
| `uv run pytest --cov` | A test fails or line and branch coverage of `src/app` drops below 90% |

Fix formatting and most lint errors with `uv run ruff format` and `uv run ruff check --fix`. The suppression rules are in [Development](docs/development.md#code-style), and the architecture tests are listed in [Testing](docs/testing.md#architecture-tests).

## CI

[`ci.yml`](.github/workflows/ci.yml) runs on every pull request and every push to `main`. It runs `make check`, scans the git history with gitleaks and `uv.lock` with OSV-Scanner, then builds the image and runs `make smoke`. [Testing](docs/testing.md#ci) describes each job. Run `make smoke` locally when you change the `Dockerfile` or the Compose files. Protect `main` with a repository ruleset so that a pull request merges only when every CI job passes and the branch is up to date.

## Commits

Write commit subjects in the [Conventional Commits](https://www.conventionalcommits.org/) style: a type, an optional scope, then a lowercase imperative summary with no trailing period.

```text
feat(auth): add password reset
chore(deps): bump astral-sh/uv
```

| Type | Use for |
|---|---|
| `feat` | New behaviour |
| `fix` | Bug fixes |
| `refactor` | Changes that keep behaviour |
| `docs`, `test`, `ci` | Documentation, tests only, the CI workflow |
| `chore` | Tooling and dependencies |

## Pull requests

Pull requests are squash-merged, so the title becomes the commit on `main`. Give it a Conventional Commits subject and keep each pull request to one change.

Before you open one:

- Run `make check`.
- If a snapshot test fails after an intended change, run `make snapshots`, review the diff and commit it. See [Testing](docs/testing.md#snapshots).
- If you changed the schema, follow the migration rules in [Development](docs/development.md#database-migrations). A merged revision is never edited.

Dependabot opens grouped pull requests weekly; split a Procrastinate bump into its own pull request ([why](docs/development.md#upgrading-procrastinate)).

## Keeping docs current

The docs describe the code as it is now. Update them in the same pull request as the change, on the page that owns the topic, and link to that page from other pages instead of repeating it:

| You change | Update |
|---|---|
| A setting | [Configuration](docs/configuration.md) and `.env.example` (a test fails if a setting is missing from it) |
| An endpoint, error code, task or event | The matching page in [docs/features](docs/features/) |
| A module, `core` or an import contract | [Architecture](docs/architecture.md) |
| A make target, command or the project layout | [README.md](README.md) and [Development](docs/development.md) |
| The image, processes or health checks | [Deployment](docs/deployment.md) |
