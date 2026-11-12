# Deployment

The template ships one container image that runs three processes: the API, the worker and migrations. Any platform that runs containers next to PostgreSQL can host it.

## Container image

Build it with `make image`, which tags it `backend-boilerplate` and passes the current commit as `GIT_SHA`.

| Property | Value |
|---|---|
| Base | multi-stage build; runtime on `python:3.14-slim-trixie`, dependencies from `uv.lock` without dev packages |
| Contents | the virtualenv with the app installed, `pyproject.toml` (Alembic reads its config from it), `migrations/` |
| User | `app`, UID 10001 |
| Port | 8000 |
| `GIT_SHA` build arg | becomes the `GIT_SHA` env var, the Sentry release and the `org.opencontainers.image.revision` label |

In your own pipeline, tag the image with the commit:

```sh
sha=$(git rev-parse HEAD)
docker build --build-arg GIT_SHA=$sha --tag registry.example.com/app:$sha .
```

CI builds and smoke-tests the image but does not push it. Add a push step for your registry.

## Processes

Run all three from the same image tag.

| Process | Command | Runs |
|---|---|---|
| API | default `CMD` | continuously, one uvicorn process per container |
| Worker | `python -m app.worker` | continuously |
| Migrate | `sh -c "alembic -x ahead=skip upgrade head && alembic -x ahead=skip check"` | once per release, before API and worker roll out |

Database connections set `idle_in_transaction_session_timeout=30s`; `statement_timeout` is 5 s for the API and 60 s for the worker. Each process uses SQLAlchemy's default pool, up to 15 connections (5 plus 10 overflow), and the worker opens Procrastinate's own connections on top. Size PostgreSQL's `max_connections` for the number of containers you run.

### API

```sh
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
  --no-proxy-headers --no-access-log --timeout-keep-alive 120 --timeout-graceful-shutdown 20
```

- `--factory` makes [`create_app`](../src/app/main.py) read the settings at startup, so invalid configuration stops the boot.
- `--no-proxy-headers` and `--no-access-log`: the app resolves the client IP and writes the access log itself.
- Keep `--timeout-keep-alive` above your load balancer's idle timeout, so the proxy never reuses a connection uvicorn already closed.

`make api` runs the same command with `--reload`. Scale the API by adding containers, not uvicorn workers. Auth rate limits are counted in memory, so each replica enforces its own.

### Worker

[`worker.py`](../src/app/worker.py) runs Procrastinate with 10 concurrent jobs, including the periodic tasks. Workers coordinate through PostgreSQL, so you can run more than one. See [Background jobs](features/background-jobs.md).

### Migrate

Run migrations as a one-off job before each release, never at app startup.

- Each run holds a PostgreSQL advisory lock, so concurrent runs wait for each other.
- `lock_timeout` is 5 s, so a migration that cannot get a table lock fails instead of queueing live traffic behind it.
- Each migration runs in its own transaction.
- `alembic check` fails the release if the models and migrations disagree.
- `-x ahead=skip` handles a rollback to an older image: if the database holds a revision the image does not know, migrate logs `migrations.skipped` and succeeds.

Because the previous release keeps running while the next one migrates, every schema change must stay compatible with the code before it. The rules are in [Development](development.md#database-migrations).

## Health endpoints

| Endpoint | Checks | Use for |
|---|---|---|
| `GET /health/live` | nothing, always `200` | the platform's liveness or container health check |
| `GET /health/ready` | `SELECT 1` within 1 s, `503` otherwise | readiness checks and an external uptime monitor |

Point restarts at `/health/live`, so a database outage does not restart every API container. Neither endpoint appears in the access log.

## Reverse proxy and client IP

Terminate TLS at a proxy or load balancer and forward plain HTTP to port 8000. In production the app adds a `Strict-Transport-Security` header to every response.

Set `TRUSTED_PROXY_HOPS` to the number of proxies in front of the app that append to `X-Forwarded-For`. The app takes the client IP from the entry that many places from the right, the one your outermost trusted proxy wrote ([`client_ip.py`](../src/app/core/http/client_ip.py)). If the header has fewer entries, or that entry is not a valid IP, it falls back to the connecting peer.

Rate limits and the access log key on the client IP: too low and every client shares the proxy's address, too high and clients can spoof theirs. Production settings reject `0`.

## Logs

Both processes log to stdout, one JSON object per line in production, with credential-like keys redacted. Collect stdout with your platform's log drain. Fields and the access log are described in [HTTP API](features/http-api.md).

## Sentry

Sentry is on when `SENTRY_DSN` is set, which production requires. See [`core/sentry.py`](../src/app/core/sentry.py).

| Reported | Not reported |
|---|---|
| API responses with a 5xx status, handled or not | 4xx responses |
| Jobs that fail their last attempt | Job attempts that will be retried |
| Jobs whose task name the worker does not know | Log records, at any level |
| Stalled jobs that run out of retries | |

Events carry the `request_id` or `job_id` tag, the release from `GIT_SHA` and the environment from `SENTRY_ENVIRONMENT`. Request bodies and local variables are never sent, and client IP headers are scrubbed.

The worker checks in to the Sentry cron monitor `worker-heartbeat` every 5 minutes (2-minute margin); Sentry creates it on the first check-in. Alert on it to learn when no worker is running.

## Graceful shutdown

| Process | On `SIGTERM` |
|---|---|
| API | stops accepting connections, gives in-flight requests 20 s, then closes the database pool and flushes Sentry |
| Worker | stops taking jobs, gives running jobs 30 s, then cancels the rest and schedules their retry, closes its resources and flushes Sentry |

Set your platform's stop timeout to at least 45 s, as [`compose.yaml`](../compose.yaml) does. If a worker dies without shutting down, jobs whose worker has had no heartbeat for 60 s are retried (or failed, if out of attempts) by `recover_stalled_jobs`, which another worker runs every 5 minutes.

## Smoke test

`make smoke` builds the image and runs the whole stack in production mode with Compose, under its own project name:

1. Starts the full stack and waits until the API is healthy.
2. Expects `200` from `/health/ready`, which checks the database.
3. Posts a signup confirmation with a common password and expects the `422` problem response, which exercises routing and request validation.
4. Waits up to 30 s for a succeeded `heartbeat` job.
5. On failure, prints the logs. Always removes the containers and volumes.

To keep the stack running for manual testing, use `make image` then `docker compose --profile full up --wait`. The API listens on `127.0.0.1:8000`, and the Sentry DSN and SMTP host are placeholders.

## Production checklist

1. Provision PostgreSQL 18 or later (the schema uses `uuidv7()`), with backups and point-in-time recovery.
2. Set `APP_ENV=production` and the remaining settings from [Configuration](configuration.md). The app refuses to start with unsafe values.
3. Put a TLS-terminating proxy in front of the API and set `TRUSTED_PROXY_HOPS`.
4. Configure an SMTP provider with TLS and a verified sender domain. See [Email](features/email.md).
5. Build the image with `GIT_SHA` and push it to your registry.
6. Run migrate, then roll out the API and the worker from the same tag.
7. Wire health checks and a stop timeout of at least 45 s, as described above.
8. In Sentry, alert on new issues, on the `worker-heartbeat` monitor and on an uptime monitor for `/health/ready`.
9. Create the first admin with `backend-boilerplate create-admin you@example.com --name "Your Name"`. It prompts for a password, so run it in an interactive shell with the API's environment, such as `docker run -it` or `exec -it`. See [Users and roles](features/users-and-roles.md).
