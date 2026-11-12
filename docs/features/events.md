# Events

An event tells other modules that something happened, without the publishing module knowing who listens. Events ride on the [job queue](background-jobs.md): each subscriber runs as its own job.

Use an event when a module must trigger work in a module that depends on it. Modules may not import each other in a cycle ([Architecture](../architecture.md)), so `users` cannot call `auth`, which imports `users`. Instead, `users` publishes `users.deactivated.v1` and `auth` subscribes to it.

| File | Role |
|---|---|
| [`core/jobs/queue.py`](../../src/app/core/jobs/queue.py) | `Event` base model and `JobQueue.publish` |
| [`core/jobs/tasks.py`](../../src/app/core/jobs/tasks.py) | The `subscriber` decorator |
| [`bootstrap.py`](../../src/app/bootstrap.py) | `SUBSCRIBERS`: which tasks run for each event type |

## Declaring an event

The publishing module owns its events.

1. Add the event to the module's `schemas.py`. It subclasses `Event` and sets an `event_type` of the form `<module>.<name>.v<N>`.

   ```python
   class UserDeactivated(Event):
       event_type: ClassVar[str] = "users.deactivated.v1"

       user_id: uuid.UUID
   ```

2. Export it from the module's `public.py` and add it to `__all__`, so subscribers can import it.
3. Run `make snapshots` and commit the updated `messages.json`.

## Publishing

Call `jobs.publish` from a service, with the session that made the change. `jobs` is the `JobQueue` the service takes as a parameter:

```python
await jobs.publish(session, UserDeactivated(user_id=user.id))
```

`publish` looks up the event type in `SUBSCRIBERS` and defers one job per subscriber task, each with the event's fields as its payload.

- An event with no subscribers defers nothing.
- A subscriber listed in `SUBSCRIBERS` that is not a registered task raises `TaskNotFound`.
- `publish` returns nothing and runs without a savepoint, so any error it raises fails the request's transaction.

## Subscribing

1. In the subscribing module's `tasks.py`, import the event through the publisher's `public.py` and decorate the handler with `subscriber`. The task name starts with the subscribing module's name, e.g. `auth.delete_sessions`.

   ```python
   @subscriber(blueprint, users.UserDeactivated, name="auth.delete_sessions")
   async def delete_sessions(context: JobContext, event: users.UserDeactivated) -> None:
       async with resources_of(context).sessionmaker.begin() as session:
           await credentials.delete_sessions(session, event.user_id)
   ```

2. List the task under the event type in `SUBSCRIBERS` in `bootstrap.py`:

   ```python
   SUBSCRIBERS = {users.UserDeactivated.event_type: ("auth.delete_sessions",)}
   ```

3. Run `make snapshots` and commit the updated `task_names.json`.

A subscriber is an ordinary [task](background-jobs.md#defining-a-task): it gets the validated event, and the same retries and [handler rules](background-jobs.md#writing-handlers) apply, so the event carries IDs, never secrets.

## Delivery guarantees

- **Transactional.** Subscriber jobs are inserted on the publisher's connection, inside its transaction. If the transaction rolls back, no subscriber runs.
- **At least once.** A subscriber job can run more than once after a retry or a stalled worker, so make handlers idempotent. Deleting a user's sessions twice is harmless, for example.
- **Independent.** Each subscriber is its own job. One that fails retries on its own schedule and does not hold back the others. Don't rely on the order in which subscribers run.
- **Asynchronous.** Subscribers run in the worker after the commit, so their effects lag behind the request. Anything that must hold when the request returns belongs in the request path too: `authenticate` rejects a deactivated user at once, and deleting the sessions is cleanup.
- **Inside the database.** Events never leave PostgreSQL. There is no broker and no external consumer.

Events follow the same [rules across releases](background-jobs.md#changing-tasks-safely) as task payloads: subscriber names are permanent and events only gain optional fields. For a breaking change, publish a new event type with the next version, such as `users.deactivated.v2`.

## Rules enforced by tests

[`test_modules.py`](../../tests/architecture/test_modules.py) fails if an event type is missing, duplicated or not `<module>.<snake_case>.v<N>`, if `SUBSCRIBERS` differs from the tasks declared with `subscriber`, or if a subscriber lacks its module prefix or the core retry strategy. Use the `run_jobs` fixture ([Testing](../testing.md)) to run subscriber jobs inside a test.

## Existing events

| Event type | Fields | Published when | Subscribers |
|---|---|---|---|
| `users.deactivated.v1` | `user_id` | An admin deactivates a user (`DELETE /v1/users/{user_id}`, see [Users and roles](users-and-roles.md)) | `auth.delete_sessions`: deletes all of the user's login sessions |
