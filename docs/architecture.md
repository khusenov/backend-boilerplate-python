# Architecture

The app is a modular monolith: one FastAPI API, one Procrastinate worker and one PostgreSQL database, shipped as one container image. Code is split by feature into modules that sit on a shared, business-free `core`.

## Why a modular monolith

- **Split by feature, not by layer.** Each module owns one business capability and its tables, and modules meet only through their `public.py`. Layers alone do not stop one feature from reading another's tables, which is the coupling that hurts as a team grows.
- **Three thin layers per module:** router, service, models. There are no repositories, mappers or DI container. The SQLAlchemy session is the unit of work, and `Depends` plus `bootstrap.py` wire the app.
- **Dependencies point inward.** `core` never imports modules, and services raise `AppError`, not HTTP exceptions. A `Protocol` port exists only where a seam has two implementations, such as `EmailSender` (SMTP in the app, an in-memory outbox in tests).
- **PostgreSQL does the infrastructure work:** data, login sessions and the job queue, so side effects commit in the same transaction as the data.
- **The trade-off:** services depend on SQLAlchemy directly, so changing the ORM means rewriting them.

## Layout of `src/app`

```text
src/app/
├── main.py        API entry point
├── worker.py      job worker entry point
├── cli.py         admin CLI
├── bootstrap.py   composition root
├── core/          shared plumbing, no business logic
└── modules/
    ├── auth/      credentials, sessions, verification codes, password reset
    └── users/     users, roles, deactivation (the reference module)
```

Imports flow one way: `main.py`, `worker.py`, `cli.py` → `bootstrap.py` → `modules` → `core`. A layer may skip the one below it, never point back up.

## Module anatomy

A module is a package under `app/modules/`. It may contain only these files:

| File | Role |
|---|---|
| `router.py` | HTTP only: parse the request, authorize with `require(...)`, call the service, convert the result to a response schema |
| `schemas.py` | Pydantic models: requests and responses (`Schema`), DTOs, job payloads (`Payload`) and events (`Event`) |
| `service.py` or `service/` | Use cases as plain async functions that take the session first, with their queries |
| `models.py` | SQLAlchemy tables, named after the module (`users`, `auth_sessions`) |
| `errors.py` | Errors that subclass a core status base (`NotFoundError`, `ConflictError`, ...) with module-prefixed codes such as `users.email_taken` |
| `tasks.py` | Procrastinate tasks and event subscribers on the module's `blueprint` (optional) |
| `public.py` | The only file other modules may import (optional) |
| `queries.py`, `domain.py` | Allowed for a module that outgrows its service (see [evolving the structure](#evolving-the-structure)) |

Classes live only in `models.py`, `schemas.py`, `errors.py` and `domain.py`; everything else is functions. `users` is the reference module to copy; [Development](development.md) walks through adding one.

### The `public.py` boundary

`public.py` is a module's API for other modules. Its `__all__` may export only three kinds of names: async functions whose first parameter is an `AsyncSession`, frozen DTOs, and event types. Everything it uses internally is imported under a private name.

```python
from app.modules.users import service as _service
from app.modules.users.schemas import UserDeactivated, UserDTO


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserDTO:
    user = await _service.get_user_unscoped(session, user_id)
    return UserDTO.model_validate(user)
```

- ORM objects never leave their module. `router.py` converts them to response schemas, `public.py` to DTOs. A DTO never doubles as a route's request or response model, so changing the API never changes a module contract.
- The caller passes its own session, so writes through `public.py` join the caller's transaction.
- `auth` depends on `users`, never the reverse. To break a cycle, move the use case to the other module, publish an [event](features/events.md), or merge the modules.

### Data ownership

- Every table has one owner: the module that declares it, or `core` for `audit_events` and Procrastinate's tables. Table names start with the owning module's name, and no other code names the table, raw SQL included. Modules write audit rows through `audit.record(...)` and jobs through `JobQueue`, never by touching core tables.
- Cross-module foreign keys declare `ON DELETE RESTRICT` or `CASCADE`. `auth` tables use `CASCADE` because sessions and credentials cannot outlive their user. Core tables never reference module tables.
- ORM relationships stay inside a module and raise on lazy load. To show another module's data in a list, make a second, batched call through its `public.py` instead of a join.
- Modules never call `commit()`, `rollback()` or raw SQL helpers, and use `text()` only in `models.py`. The transaction belongs to the request or the task that opened it.

## The `core` package

`core` holds everything that is not a business capability. Its `__init__.py` stays empty, so importing one core file never loads the rest.

| Path | Role |
|---|---|
| [`config.py`](../src/app/core/config.py) | `Settings`, the only way to read the environment ([Configuration](configuration.md)) |
| [`db.py`](../src/app/core/db.py) | `Base` (UUIDv7 `id`, `created_at`, `updated_at`, constraint naming), `flush()` that maps constraint names to errors, `delete_in_batches()` |
| `errors.py` | `AppError` and the status bases modules subclass (`NotFoundError`, `ConflictError`, ...) |
| `security.py` | Roles, permissions, `Principal` ([Users and roles](features/users-and-roles.md)) |
| `resources.py` | `Resources`: the per-process engine, sessionmaker, job queue, email sender, rate limiter and hashing pool |
| `schemas.py`, `pagination.py` | The `Schema` base (unknown fields rejected, frozen), shared field types, cursor pages ([HTTP API](features/http-api.md)) |
| `audit.py` | The `audit_events` table and `record()` |
| `email.py` | `EmailSender` and `SmtpEmailSender` ([Email](features/email.md)) |
| `logging.py`, `sentry.py` | structlog setup with credential redaction, Sentry setup |
| `http/` | Dependencies, authorization, middleware, problem responses, OpenAPI tweaks, health routes |
| `jobs/` | `JobQueue`, `Payload`/`Event` bases, task decorators and retry strategy, core periodic tasks ([Background jobs](features/background-jobs.md)) |

## Enforced rules

`make check` enforces these rules, and a violation fails the build ([CONTRIBUTING.md](../CONTRIBUTING.md)).

### Import contracts

[import-linter](https://import-linter.readthedocs.io/) contracts live in `pyproject.toml` under `[tool.importlinter]`.

| Contract | Rule |
|---|---|
| Entrypoint chain | The import chain above, and every package under `app` is in it |
| HTTP stays in routers and core.http | In `core` and `modules`, only `core/http/` imports FastAPI and Starlette; module `router.py` files may import FastAPI and `core.http` |
| Procrastinate stays in core.jobs and tasks | Only `core/jobs/` and module `tasks.py` files import Procrastinate |
| Modules meet only through public.py | A module imports another only through its `public.py` |
| Inside a module | `router`, `tasks`, `public` → `service` → `queries`, `domain` → `schemas`, `models`, `errors` |
| Modules are acyclic | No import cycles between modules |
| Routers leave queries to services | `router.py` imports neither SQLAlchemy nor `models.py` |

### Banned APIs

Ruff's `banned-api` setting blocks calls that bypass the architecture:

| Banned | Use instead |
|---|---|
| `os.environ`, `os.getenv` | `Settings` |
| `fastapi.BackgroundTasks`, `asyncio.create_task`, `asyncio.ensure_future` | `JobQueue.defer(...)`, or `asyncio.TaskGroup` for in-request concurrency |
| `src.app...` imports | `app...` |
| `typing.no_type_check` | A per-line `# pyright: ignore[rule]` |

### Architecture tests

`tests/architecture/` checks what import rules cannot ([Testing](testing.md)):

- module anatomy
- table and error-code naming
- `public.py` export rules
- only tasks send email
- every route requires a permission or is anonymous
- `bootstrap.py` lists every module, blueprint and subscriber
- mapper and schema conventions

## Request lifecycle

Middleware runs in this order, outermost first. The order is deliberate: every response, errors included, passes through the layers that tag it.

1. `RequestIdMiddleware` generates a UUIDv7 request ID, binds it to the log context and returns it as `X-Request-ID`.
2. `ClientIpMiddleware` replaces the peer address with the client IP from `X-Forwarded-For`, trusting `TRUSTED_PROXY_HOPS` proxies ([Deployment](deployment.md)).
3. `AccessLogMiddleware` logs one `http.request` line per request, skipping `/health/`.
4. `SecurityHeadersMiddleware` adds `nosniff`, `no-store`, `no-referrer`, and HSTS in production.
5. `CORSMiddleware` allows `CORS_ORIGINS` with credentials.
6. `UnhandledErrorMiddleware` turns an unexpected exception into a bare 500 problem response, then re-raises it so it is logged and reported. It sits inside CORS so browsers can read the 500.
7. `BodyLimitMiddleware` rejects bodies over 1 MiB with 413.

Inside the list, FastAPI's exception handlers render `AppError`, `HTTPException` and validation errors as `application/problem+json` ([HTTP API](features/http-api.md)). Then the route's dependencies run, reading `Settings` and `Resources` from the lifespan state through [`core/http/deps.py`](../src/app/core/http/deps.py):

- **Authorization.** A route declares `dependencies=[require(Permission.X)]`, or its router uses `route_class=AnonymousRoute`. A test fails if a route does neither. `require` resolves `CurrentPrincipal`, which reads a bearer token or the session cookie (a cookie on an unsafe method also needs a trusted `Origin`, the CSRF check) and calls the `authenticate` function from `Resources` ([Authentication](features/auth.md)).
- **Transaction.** `DbSession` opens one transaction per request. `GET`, `HEAD` and `OPTIONS` run it read-only. It commits after the endpoint returns but before the response is sent, so a failed commit becomes a 500 instead of a false success. Any exception rolls it back.
- **Services** raise `AppError` subclasses. `flush(session, {"uq_users_email": EmailTakenError})` turns a constraint violation into a domain error.
- **Side effects** go through `jobs.defer(session, payload)` or `jobs.publish(session, event)`, which insert the job on the request's connection. It commits or rolls back with the data ([Background jobs](features/background-jobs.md)).

## Entry points

| File | Run with | Does |
|---|---|---|
| [`main.py`](../src/app/main.py) | `uvicorn app.main:create_app --factory` | Builds the app: logging, Sentry, `Resources` in the lifespan, `/health` and `/v1` routers. OpenAPI is off in production. |
| [`worker.py`](../src/app/worker.py) | `python -m app.worker` | Runs the Procrastinate worker with its own `Resources` ([Background jobs](features/background-jobs.md)). |
| [`cli.py`](../src/app/cli.py) | `backend-boilerplate create-admin EMAIL` | Typer CLI for operator tasks ([Users and roles](features/users-and-roles.md)). |
| [`bootstrap.py`](../src/app/bootstrap.py) | imported by the others | The composition root (see below). |

`bootstrap.py` exists because `core` cannot import modules. It is the one place that knows every module, so it hands `auth`'s `authenticate` function to `core.http` through `Resources` and maps event types to subscriber tasks. A new module is registered there and its routers in `main.py`.

## Evolving the structure

| Wait for this trigger | Then change |
|---|---|
| A `service.py` passes ~400 lines or repeats a query three times | Split it into a `service/` package by use case (as `auth` does), or add `queries.py` |
| Business rules outgrow the service: a state machine, or one invariant in two or more write paths | Add a pure `domain.py` to that module only |
| An external system gets a second implementation | Put a `Protocol` port in front of it |
| More than one API replica | Move rate limiting from in-process memory to a shared `limits` storage such as Redis |
| Database connections above ~70% of `max_connections` | Add PgBouncer, then a read replica |
| Logs and Sentry stop answering latency questions | Add OpenTelemetry traces and metrics |
| A route moves money | Require an `Idempotency-Key` from its first release |
| A module has its own team, hurts API latency, or sits behind a compliance boundary (two of these) | Extract it: own database, `public.py` over HTTP, events through a broker |
