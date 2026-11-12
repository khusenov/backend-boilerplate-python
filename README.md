# backend-boilerplate

A FastAPI backend template for Python 3.14. It is a modular monolith: one API process, one job worker and one PostgreSQL database, shipped as one container image. Clone it, rename it, and start adding modules.

## Features

- **Modular monolith** with boundaries enforced by import-linter and architecture tests, not by convention.
- **Authentication:** email and password with argon2, cookie sessions for browsers and bearer tokens for other clients, signup with emailed confirmation codes, password reset and change, email change, and rate limits.
- **Users and roles:** `admin` and `member` roles mapped to permissions, a `require(...)` permission check on every route that isn't explicitly anonymous, audit events and user deactivation.
- **HTTP API:** `/v1` versioning, RFC 9457 problem details with stable error codes, cursor pagination, request IDs, CORS, security headers and a body size limit.
- **Background jobs** on [Procrastinate](https://procrastinate.readthedocs.io/), stored in PostgreSQL and deferred in the same transaction as your data, with periodic tasks, a heartbeat and retention pruning.
- **Events** between modules, delivered through the job queue so each subscriber runs as its own job.
- **Email** over SMTP, sent only from background tasks, with Mailpit for local development.
- **Data:** SQLAlchemy 2 async with psycopg 3, and Alembic migrations with numbered revisions.
- **Operations:** structured logs with structlog, Sentry for errors and the worker heartbeat, health probes, graceful shutdown, a non-root container image and a smoke test.
- **Tooling:** uv, Ruff and basedpyright (strict on `src/`); pytest against a real PostgreSQL with a 90% branch coverage floor; CI with secret and vulnerability scanning.

## Requirements

- [uv](https://docs.astral.sh/uv/) 0.12.7 or later. It installs Python 3.14 if you don't have it.
- Docker with Compose, for the local PostgreSQL and Mailpit. The tests start their own PostgreSQL container, so Docker must be running for `make check`.
- GNU Make.

## Quick start

```sh
uv sync
make dev
```

Then open http://127.0.0.1:8000/docs for the interactive API docs and http://127.0.0.1:8025 for the Mailpit inbox.

See [Development](docs/development.md) for the full local workflow and [CONTRIBUTING.md](CONTRIBUTING.md) for what `make check` enforces.

## Make targets

| Target | What it does |
|---|---|
| `make dev` | Creates `.env` from `.env.example` if missing, starts PostgreSQL and Mailpit, migrates, then runs the API and the worker |
| `make api` | Runs the API on port 8000 with reload |
| `make worker` | Runs the job worker |
| `make db` | Starts PostgreSQL on `127.0.0.1:5433` |
| `make mailpit` | Starts Mailpit (SMTP on 1025, inbox on 8025) |
| `make migrate` | Applies all migrations to the local database |
| `make migration name="add orders table"` | Generates the next numbered revision from the models |
| `make check` | Lockfile, format, lint, types, import contracts and tests with coverage |
| `make snapshots` | Regenerates the architecture snapshots after an intended change |
| `make image` | Builds the container image and records the current commit as `GIT_SHA` |
| `make smoke` | Builds the image, runs the full stack in Compose and probes it |

## Project layout

```text
.
├── src/app/
│   ├── main.py          API entry point
│   ├── worker.py        job worker entry point
│   ├── cli.py           admin CLI
│   ├── bootstrap.py     composition root
│   ├── core/            shared plumbing: config, db, errors, http, jobs, email, logging
│   └── modules/
│       ├── auth/        credentials, sessions, verification codes, password reset
│       └── users/       users, roles, deactivation (the reference module)
├── migrations/          Alembic environment and revisions
├── tests/               mirrors src/app, plus architecture tests and snapshots
├── docs/                developer documentation
├── Dockerfile
├── compose.yaml         local services and the full production-mode stack
└── Makefile
```

[Architecture](docs/architecture.md) explains the module anatomy and the rules between layers.

## Start a new project

The Python package stays `app`, so no imports change. Rename the project in these places:

1. [`pyproject.toml`](pyproject.toml): the project `name` and the CLI command under `[project.scripts]`. Then run `uv lock` to update `uv.lock`.
2. [`src/app/main.py`](src/app/main.py): the `title` of the app, which is the OpenAPI title. Update the two assertions in [`tests/architecture/test_boot.py`](tests/architecture/test_boot.py) and run `make snapshots` to refresh `openapi.json`.
3. [`Makefile`](Makefile): the `--tag` in the `image` target and the `--project-name` in `SMOKE`.
4. [`compose.yaml`](compose.yaml): the `image` under `x-app`. It must match the tag that `make image` builds. Add a top-level `name:` if you want a fixed Compose project name instead of the directory name.
5. The README title, the CLI command and image tag in `docs/`, and the copyright holder in [`LICENSE`](LICENSE).
6. [`SECURITY.md`](SECURITY.md): replace the reporting section with your own contact.

Check that nothing is left, then run the gates. The only expected match is the grep line in this README:

```sh
grep -rn backend-boilerplate --exclude-dir=.venv --exclude-dir=.git .
make check
```

Before the first deploy, work through the [production checklist](docs/deployment.md#production-checklist).

## Documentation

| Page | Covers |
|---|---|
| [Architecture](docs/architecture.md) | Modules, `core`, import contracts, request lifecycle, entry points |
| [Development](docs/development.md) | Local setup, migrations, adding modules, endpoints and tasks, CLI |
| [Testing](docs/testing.md) | Test layout, fixtures, database tests, snapshots, coverage, CI |
| [Configuration](docs/configuration.md) | Every setting, production validation, secrets |
| [Deployment](docs/deployment.md) | Container image, processes, health checks, proxies, Sentry, checklist |
| [HTTP API](docs/features/http-api.md) | Versioning, routers, errors, validation, pagination, OpenAPI |
| [Authentication](docs/features/auth.md) | Sessions and tokens, signup, codes, password and email flows, rate limits |
| [Users and roles](docs/features/users-and-roles.md) | Users endpoints, permissions, audit events, admin CLI |
| [Background jobs](docs/features/background-jobs.md) | Tasks, deferring, retries, periodic tasks, the worker |
| [Events](docs/features/events.md) | Publishing and subscribing between modules |
| [Email](docs/features/email.md) | Sending mail, messages, Mailpit, SMTP |
| [Contributing](CONTRIBUTING.md) | Contributor setup, what `make check` and CI enforce, commit and PR style |
| [Security](SECURITY.md) | Reporting vulnerabilities, security features, what a deployer must configure |

## License

MIT. See [LICENSE](LICENSE). The password blocklist, [`common_passwords.txt`](src/app/modules/auth/common_passwords.txt), has its own MIT license in `common_passwords.LICENSE`; keep it when you change the copyright line.
