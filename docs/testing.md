# Testing

The suite runs against a real PostgreSQL 18 started by [testcontainers](https://testcontainers.com/), one container per run. You need Docker running. The suite always starts its own container and never uses the Compose database from `make db`, so you don't need `.env` or any other local service.

## Running tests

| Command | Runs |
|---|---|
| `make check` | Every gate, ending with `uv run pytest --cov` |
| `uv run pytest` | The whole suite, without the coverage floor |
| `uv run pytest tests/modules/users/test_service.py::test_a_duplicate_email_is_taken` | One test; drop the `::` part for one file, or name a directory |

pytest runs in strict mode and turns every warning into an error. Tests are plain `async def` functions; `asyncio_mode = "auto"` runs them on one session-scoped event loop.

`make check` measures branch coverage of `src/app` and fails below 90%. The report lists the missing lines of every file that isn't fully covered. `test_suppressions.py` rejects a lower floor and any exclusion pattern.

## Layout

| Path | Holds |
|---|---|
| `tests/core/`, `tests/modules/<name>/` | Tests for `src/app/core` and for each module, mirroring the source layout |
| `tests/test_main.py`, `test_worker.py`, `test_cli.py`, `test_bootstrap.py` | Tests for the entry points |
| `tests/architecture/` | Rules about the codebase itself, and the snapshots |

## Fixtures

Shared fixtures live in [`tests/conftest.py`](../tests/conftest.py); a module's test directory can add its own `conftest.py`, as auth does for `resume_hashing`. Two run for every test: `hermetic_environment` removes every settings variable, so `.env` and your shell never leak in, and `offline_sentry` swaps Sentry's HTTP transport for one that records events.

| Fixture | Gives you |
|---|---|
| `client`, `app` | An `httpx.AsyncClient` on `https://testserver` running `create_app(settings, resources)` with its real lifespan |
| `session` | An `AsyncSession` inside the test's transaction |
| `sessionmaker`, `connection`, `engine`, `database_url` | The same database at lower levels, down to the container's URL |
| `settings`, `production_settings` | `Settings` for `app_env="test"`, and a set that passes production validation |
| `environment` | A minimal valid test configuration (`APP_ENV`, `SECRET_KEY`, `DATABASE_URL`, `SMTP_HOST`, `EMAIL_FROM`) set as environment variables, for code that reads them itself |
| `resources`, `outbox` | The `Resources` the app runs on, with in-memory email and rate limiting; read sent mail from `outbox.sent` |
| `run_jobs` | Runs deferred jobs once; see [database isolation](#database-isolation) |
| `scratch_tables` | Creates throwaway tables from [`tests/scratch.py`](../tests/scratch.py) for tests that need tables no module owns |

## Helpers

| Module | Use it for |
|---|---|
| `factories.py` | `make_user`, `make_credential`, `make_login_session`, `make_code`, `make_reset_token`, `make_audit_event`. Each takes the session plus whatever the row needs (a user, or `secret_key` and an email) and accepts `**overrides`. `make_login_session`, `make_code` and `make_reset_token` return the plaintext token or code. Also `count_rows`, `principal_for` and `wrong_code` |
| `clients.py` | `bearer(token)` headers, `problem(response)` as `(status, code)`, `serve(app)` for a custom app |
| `outbox.py` | `code_in(email)` and `token_in(email)` pull a code or token out of a sent email |
| `jobs.py` | Test-only tasks, job table queries, `work_until_finished` for real worker runs |
| `audit.py`, `logs.py`, `sentry.py`, `statements.py` | Reading audit rows, JSON log lines, captured Sentry events and executed SQL |
| `environment.py`, `migrations.py` | Setting test environment variables, and the Alembic config the schema build uses |

## Writing a test

Prefer tests that go through HTTP. Build data with the factories, call the endpoint, assert on the response:

```python
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.clients import bearer, problem
from tests.factories import make_login_session, make_user


async def test_members_cannot_create_users(
    client: AsyncClient, session: AsyncSession
) -> None:
    member = await make_user(session)
    headers = bearer(await make_login_session(session, member))
    body = {"email": "new@example.com", "name": "New User"}

    response = await client.post("/v1/users", headers=headers, json=body)

    assert problem(response) == (403, "permission_denied")
```

- Call service functions directly when a branch is hard to reach over HTTP.
- Fake only what leaves the process, such as email and Sentry, and swap fakes in through `Resources`. The suite patches no application code.
- To test expiry, create rows with `expires_at` in the past. Don't move the clock.

## Database isolation

The schema is built once per run with `alembic upgrade head`, so every run also exercises the migrations. Each test runs inside one transaction on one connection. The `session` fixture and the app's sessions join it with `join_transaction_mode="create_savepoint"`, so a commit in application code becomes a savepoint. Teardown rolls back the whole transaction. Data you create through `session` is visible to `client` straight away.

`run_jobs()` processes deferred jobs on the test's own connection, so you can assert on a task's effects without committing; it skips periodic tasks and re-raises the first task error.

Mark a test `@pytest.mark.committed` when work must happen on another connection, such as a real worker run, concurrent requests or a CLI command. Its sessions commit for real, and teardown truncates every table except `alembic_version`.

## Architecture tests

`tests/architecture/` enforces the rules that [Architecture](architecture.md) explains. Import boundaries between modules are checked separately, by `lint-imports`.

| File | Checks |
|---|---|
| `test_modules.py` | `public.py` exports, module file set, no transactions, raw SQL or email outside their owners, module-prefixed table, error, task and event names, registration in `bootstrap.py` |
| `test_mappers.py` | UUIDv7 keys, indexed foreign keys, explicit cross-module `ON DELETE`, `timestamptz`, no lazy loads |
| `test_schemas.py` | Schemas keep the base config, request strings and lists have a max length, JSON names are snake_case |
| `test_routes.py` | Every route requires a permission or is marked anonymous |
| `test_migrations.py` | One linear history, upgrade only, models match migrations, migration locking |
| `test_boot.py`, `test_env_example.py` | The app boots, bad settings fail boot, `.env.example` lists every setting once |
| `test_image.py`, `test_compose.py`, `test_workflows.py` | The Dockerfile, Compose files and pinned GitHub Actions |
| `test_suppressions.py`, `test_packages.py` | Lint and type ignores stay per line, gate settings stay put, `app/core/__init__.py` stays empty |

## Snapshots

`test_snapshots.py` compares the OpenAPI schema, task names, error codes, task payload and event shapes, and migration revisions with the JSON files in `tests/architecture/snapshots/`. When you change one on purpose, the test fails and says so: run `make snapshots`, review the diff and commit it with your change.

Three rules are checked before `make snapshots` rewrites anything, so it can't override them:

- An error code, once published, keeps its status and never disappears.
- An existing task payload or event never gains a required field, so queued jobs and old publishers keep validating.
- A published migration revision is never removed or given a new parent.

## CI

[`ci.yml`](../.github/workflows/ci.yml) runs on every pull request and every push to `main`.

| Job | Does |
|---|---|
| `check` | `make check` |
| `gitleaks`, `osv-scanner` | Scan the full git history for secrets, and `uv.lock` for known vulnerabilities |
| `smoke` | `make smoke` once the other three pass; see [Deployment](deployment.md) |
