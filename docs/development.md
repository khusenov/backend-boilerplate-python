# Development

## Local setup

1. Install the dependencies with `uv sync`. uv fetches Python 3.14 if you don't have it.
2. Start everything with `make dev`.

`make dev` creates `.env` from `.env.example` if it is missing, starts PostgreSQL and Mailpit in Docker, applies the migrations, and runs the API and the worker together.

Ctrl-C stops the API and the worker. The containers keep running: stop them with `docker compose down`, and add `--volumes` to delete the local database.

| Service | Address |
|---|---|
| API | http://127.0.0.1:8000 |
| OpenAPI docs | http://127.0.0.1:8000/docs and `/redoc` (not served in production) |
| PostgreSQL | `127.0.0.1:5433`, user, password and database `app` |
| Mailpit SMTP | `127.0.0.1:1025` |
| Mailpit inbox | http://127.0.0.1:8025 |

PostgreSQL listens on 5433 so it doesn't clash with a local install on 5432. A fresh database has no users. Sign up through `POST /v1/auth/signup` (the code arrives in Mailpit), or create an admin with the [CLI](#cli-commands) to use the admin endpoints.

To run the processes in separate terminals, use `make api` and `make worker`. The API reloads when a file under `src/` changes. The worker doesn't, so restart it after you change a task.

## The `.env` file

The app reads settings from the process environment only. It never opens `.env` itself: the Make targets pass it with `uv run --env-file .env`, and you do the same when you run a command by hand:

```sh
uv run --env-file .env alembic current
```

`.env.example` holds working local values for every setting, and a test fails if a setting is missing from it. Each setting is described in [Configuration](configuration.md).

## Database migrations

Alembic lives in [`migrations/`](../migrations) and is configured under `[tool.alembic]` in `pyproject.toml`. Revisions are numbered `0001`, `0002`, and so on.

To change the schema:

1. Edit the models in the module's `models.py`. A new models module must be listed in `MODELS` in [`bootstrap.py`](../src/app/bootstrap.py), or Alembic won't see its tables.
2. Run `make migrate` so the local database is at the head. Autogenerate compares the models with this database.
3. Run `make migration name="add invoices table"`. This writes `migrations/versions/0009_add_invoices_table.py` and formats it with Ruff.
4. Read the generated file. Autogenerate sees a renamed column as a drop plus an add, and it misses some changes.
5. Run `make migrate` to apply it, then `make snapshots` to record the revision.

Tests in [`test_migrations.py`](../tests/architecture/test_migrations.py) and the revision snapshot check these rules:

- **One linear history.** There is a single head, with no branches or merge revisions.
- **Upgrade only.** Revisions have no `downgrade()`. You fix a mistake with a new revision.
- **Merged revisions are final.** Never edit, renumber or delete one. A database that applied a revision never runs it again, so an edit reaches only fresh databases. The snapshot catches a deleted or renumbered revision, but not an edited body.
- **Models match migrations.** The test suite runs `alembic check` against a migrated database, server defaults included. Procrastinate's `procrastinate_*` tables are left out of the comparison.

In production, migrations run as a separate step before the new code starts, with a lock and timeouts described in [Deployment](deployment.md#migrate). The old code keeps running meanwhile, so it must work on the new schema. For changes that would break it:

- Rename or retype a column in stages: add the new column, backfill it, move readers and writers to it, then drop the old column in a later release.
- Build a large index with `CREATE INDEX CONCURRENTLY` inside `with op.get_context().autocommit_block():`.

### When two branches add a revision

Revision numbers come from counting files, so two branches can both create `0009`. After you rebase on the branch that merged first, tests fail with `Revision 0009 is present more than once`, and `make migrate` only warns and applies one of them. To fix it:

1. Delete your revision file.
2. Reset the local database with `docker compose down --volumes`, then run `make migrate`. Your database still records your old `0009` as applied, so Alembic would skip the new one.
3. Generate the revision again with `make migration`.

## Adding a module

1. Copy [`src/app/modules/users`](../src/app/modules/users) to `src/app/modules/<name>` and remove the files you don't need. Each file's role is in [Architecture](architecture.md).
2. Start every table name, error code, task name and event type with the module name. Tables use an underscore (`invoices`, `invoices_lines`). Error codes, task names and event types use a dot (`invoices.not_found`, `invoices.send_receipt`), and event types end with a version (`invoices.paid.v1`). Architecture tests check this.
3. In [`bootstrap.py`](../src/app/bootstrap.py), add the models module to `MODELS` and the task blueprint to `BLUEPRINTS`.
4. In [`main.py`](../src/app/main.py), include the router with `prefix="/v1"`.
5. Add the module's permissions to `Permission` and `ROLE_PERMISSIONS` in [`core/security.py`](../src/app/core/security.py).
6. Generate the migration as described above.
7. Add tests under `tests/modules/<name>/`, run `make snapshots`, review the diff, then run `make check`.

## Adding an endpoint

1. Define request and response models in `schemas.py`. They subclass `Schema` from [`core/schemas.py`](../src/app/core/schemas.py).
2. Write the use case as a plain function in `service.py` that takes the session.
3. Add the route to `router.py`. Every route either requires a permission or sits on a router whose `route_class` is `AnonymousRoute`, and a test fails otherwise:

```python
@router.get("/{user_id}", dependencies=[require(Permission.USERS_READ)])
async def get_user(
    user_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> UserRead:
    user = await service.get_user(session, principal, user_id)
    return UserRead.model_validate(user)
```

4. Run `make snapshots` and review the change to `tests/architecture/snapshots/openapi.json`.

Errors, pagination and validation are covered in [HTTP API](features/http-api.md). Permissions and roles are covered in [Users and roles](features/users-and-roles.md).

## Adding a background task

1. Declare a `Payload` subclass with a `task_name` in the module's `schemas.py`.
2. Write the handler in `tasks.py` with `@task(blueprint, Payload)`.
3. Defer it with `JobQueue.defer` inside the request's transaction.
4. Run `make snapshots` to record the task name and payload shape.

The details are in [Background jobs](features/background-jobs.md). To fan out to several handlers, publish an [event](features/events.md) instead.

## CLI commands

The project installs a `backend-boilerplate` command, defined in [`cli.py`](../src/app/cli.py) with Typer:

```sh
uv run --env-file .env backend-boilerplate create-admin ada@example.com --name Ada
```

`create-admin` prompts for the password twice; roles are covered in [Users and roles](features/users-and-roles.md). Run `backend-boilerplate --help` to list all commands.

To add a command, write a sync Typer function in `cli.py` that wraps its async work in `asyncio.run()`.

## Code style

`make check` runs these tools; CI runs the same target (see [CONTRIBUTING.md](../CONTRIBUTING.md)). It changes nothing: fix what it reports with `uv run ruff format` and `uv run ruff check --fix`.

- **Ruff** formats the code and lints it with the rule set in `pyproject.toml`.
- **basedpyright** type-checks `src/` in strict mode and everything else in standard mode. Warnings fail the run.
- **import-linter** and the banned-API list keep modules apart. See [Architecture](architecture.md).

Silence a rule on a single line and name it: `# noqa: S311` or `# pyright: ignore[reportAny]`. A test rejects `# type: ignore`, file-wide directives, ignoring the banned-API rule in `pyproject.toml` and coverage exclusions. basedpyright fails on an ignore that is no longer needed.

## Dependency updates

- Add a package with `uv add <package>` (`uv add --dev` for tooling). Upgrade one with `uv lock --upgrade-package <package>`. Commit `uv.lock`: `make check` fails when it is out of date.
- Dependabot opens grouped weekly pull requests for Python packages, GitHub Actions, Dockerfile images and Compose images. It skips minor and major updates of the `python` base image and major updates of `postgres`, because those need a deliberate upgrade.
- Procrastinate, the Sentry SDK and Ruff are pinned to exact versions, and Dependabot's weekly Python pull request bumps those pins too. A Procrastinate bump fails the tests until you add its revisions as described in [Upgrading Procrastinate](#upgrading-procrastinate), so move it to its own pull request.
- GitHub Actions are pinned by commit SHA, and a test enforces it.

### Upgrading Procrastinate

Procrastinate is pinned to an exact version because its schema lives in the migrations. The `0005` revision applies the vendored `procrastinate-3.10.0.sql`. Change the pin in `pyproject.toml` and run `uv lock`. Then `test_every_newer_procrastinate_migration_has_a_revision` lists the SQL migrations the new version adds. Add one revision per file:

- The `pre` files go in the release that installs the new version.
- The `post` files go in the next release, once no worker of the old version is running.

Generate each revision with `make migration`, then replace its body:

```python
from importlib import resources

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
procrastinate_migration: str = "<file name>.sql"


def upgrade() -> None:
    sql = resources.files("procrastinate.sql.migrations") / procrastinate_migration
    op.execute(sql.read_text(encoding="utf-8"))
```
