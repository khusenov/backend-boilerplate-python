# Background jobs

Work that should not block a request, such as sending email, runs as a [Procrastinate](https://procrastinate.readthedocs.io/) job. Jobs live in PostgreSQL tables (`procrastinate_jobs` and related tables), so there is no broker to run: the API inserts rows and the worker process picks them up.

| File | Role |
|---|---|
| [`core/jobs/queue.py`](../../src/app/core/jobs/queue.py) | `Payload` and `Event` base models, `JobQueue.defer` and `JobQueue.publish` |
| [`core/jobs/tasks.py`](../../src/app/core/jobs/tasks.py) | `task`, `subscriber` and `periodic_task` decorators, the retry strategy, Sentry reporting |
| [`core/jobs/periodic.py`](../../src/app/core/jobs/periodic.py) | Core periodic tasks: heartbeat, stalled-job recovery, pruning |
| `modules/<name>/tasks.py` | A module's `Blueprint` and its task handlers |
| [`bootstrap.py`](../../src/app/bootstrap.py) | `BLUEPRINTS` and `SUBSCRIBERS`: every task and subscription the app knows |
| [`worker.py`](../../src/app/worker.py) | The worker process |

Inside `core` and the modules, only `core/jobs` and `modules/*/tasks.py` may import `procrastinate`; see [Architecture](../architecture.md) for the import-linter contracts.

## Running the worker

Locally, `make worker` starts the worker and `make dev` starts it next to the API. In a container, run `python -m app.worker`, as the `worker` service in `compose.yaml` does. Run as many worker processes as you need; they coordinate through the database ([Deployment](../deployment.md)).

Each worker process:

- runs up to 10 jobs at once;
- listens for new jobs with PostgreSQL `LISTEN/NOTIFY`;
- opens database connections with a 60 s statement timeout (the API uses 5 s) and a 30 s idle-in-transaction timeout;
- on `SIGTERM`, stops taking jobs and gives running jobs 30 s to finish; jobs still running after that are cancelled and retried.

## Defining a task

1. Add the payload to the module's `schemas.py`. It subclasses `Payload` and names its task. Task names start with the module name.

   ```python
   class SendWelcomeEmail(Payload):
       task_name: ClassVar[str] = "users.send_welcome_email"

       user_id: uuid.UUID
   ```

2. Write the handler in the module's `tasks.py` with the `task` decorator. It receives the job context and the validated payload, never raw keyword arguments.

   ```python
   @task(blueprint, SendWelcomeEmail)
   async def send_welcome_email(context: JobContext, payload: SendWelcomeEmail) -> None:
       resources = resources_of(context)
       async with resources.sessionmaker.begin() as session:
           user = await service.get_user_unscoped(session, payload.user_id)
       if user.active:
           await resources.email.send(to=user.email, subject="Welcome", text=...)
   ```

3. If the module has no `tasks.py` yet, create one with `blueprint = Blueprint()` and add that blueprint to `BLUEPRINTS` in `bootstrap.py`.
4. Run `make snapshots` and commit the updated `task_names.json` and `messages.json` (see [changing tasks safely](#changing-tasks-safely)).

- `resources_of(context)` returns the shared `Resources` (sessionmaker, email sender, job queue); `settings_of(context)` returns `Settings`.
- Open your own transaction with `sessionmaker.begin()`.
- Pass `queueing_lock="..."` to `task` to allow at most one waiting job of that task.

## Deferring a task

Routes get a `JobQueue` from the `Jobs` dependency and pass it to the service, which calls `defer`:

```python
async def create_user(session: AsyncSession, jobs: JobQueue, data: UserCreate) -> User:
    user = User(email=data.email, name=data.name)
    session.add(user)
    await flush(session, {"uq_users_email": EmailTakenError})
    await jobs.defer(session, SendWelcomeEmail(user_id=user.id))
    return user
```

`defer` inserts the job on the session's own connection, inside the caller's transaction. The job exists only if that transaction commits: a failed request drops its jobs, and the worker never sees a job for a row that was rolled back.

- `defer` returns the job ID.
- It raises `TaskNotFound` if the payload names a task that is not registered.
- It runs inside a savepoint, so an `AlreadyEnqueued` error from a queueing lock leaves the caller's transaction usable.

To fan out to several handlers, publish an [event](events.md) instead.

## Retries and failures

Every module task, periodic ones included, uses the same retry strategy for any exception. An architecture test enforces it.

| Setting | Value |
|---|---|
| Attempts | 14 in total |
| First retry | after 2 s |
| Backoff | doubles each attempt, capped at 1 hour |

That covers roughly three hours of outage before a job fails for good. The core periodic tasks in `periodic.py` do not retry; their next tick runs instead. Only the final failure reaches Sentry, tagged with `job_id`, so an error that a retry fixes stays quiet. Jobs with an unknown task name are reported too. Log lines written during a job carry `job_id`.

## Periodic tasks

Periodic tasks run on a cron schedule. A module declares one with `periodic_task`; the handler receives only the context:

```python
@periodic_task(blueprint, name="auth.prune_expired_rows", cron=HOURLY)
async def prune_expired_rows(context: JobContext) -> None: ...
```

`periodic_task` gives the task a queueing lock named after it, so a slow run never piles up a backlog. Every worker schedules periodic jobs, and Procrastinate makes sure each tick runs once.

| Task | Schedule | What it does |
|---|---|---|
| `heartbeat` | every 5 min | Checks in with the Sentry cron monitor `worker-heartbeat` |
| `recover_stalled_jobs` | every 5 min | Retries jobs whose worker has sent no heartbeat for 60 s, for example after a crash; fails them and reports to Sentry when out of attempts |
| `prune_audit_events` | hourly | Deletes audit events older than 365 days |
| `prune_finished_jobs` | hourly | Deletes jobs that finished more than 7 days ago |
| `auth.prune_expired_rows` | hourly | Deletes expired login sessions, verification codes and reset tokens |

`heartbeat` and `recover_stalled_jobs` run ahead of queued module jobs, so a long backlog does not delay them. With `SENTRY_DSN` set, the `worker-heartbeat` monitor alerts you when no worker has checked in for a 5-minute slot (with a 2-minute margin). That catches a worker that is down or stuck, which no error report would.

### Pruning

Pruning deletes in batches of up to 1000 rows, each batch in its own transaction, so no transaction stays open for long. To prune a new table, call `delete_in_batches` from [`core/db.py`](../../src/app/core/db.py) in a periodic task, and index the column the condition filters on.

## Writing handlers

- **Payloads carry IDs, never secrets.** The task loads the row and mints any code or token itself, so nothing sensitive sits in the jobs table.
- **Handlers are idempotent.** A job can run more than once after a retry or a stalled worker. The auth tasks, for example, derive the same code from a stored seed on every run.
- **Keep provider calls outside transactions.** Load what you need, close the transaction, then send the email or call the API.
- **Only tasks send email.** An architecture test enforces it; see [Email](email.md).

## Changing tasks safely

During a deploy, old and new code share one queue, so jobs written by one version are run by the other.

- Task names are permanent. Renaming one strands its queued jobs. `task_names.json` snapshots them.
- Payloads and events only gain optional fields. Messages ignore unknown fields, so an old worker can run a newer job. `messages.json` snapshots their shapes, and a test fails if a field becomes required.
- Remove a task or a field only after every job that uses it has finished.

See [Testing](../testing.md) for snapshot tests and the `run_jobs` fixture that runs deferred jobs inside a test.

## Upgrading Procrastinate

Procrastinate's schema lives in the Alembic history, so its version is pinned. To upgrade it, follow [Upgrading Procrastinate](../development.md#upgrading-procrastinate).
