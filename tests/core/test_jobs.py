import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient
from procrastinate import JobContext
from procrastinate.exceptions import AlreadyEnqueued, TaskNotFound
from procrastinate.jobs import Job
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap import job_app
from app.core.audit import AuditEvent
from app.core.config import Settings
from app.core.db import DELETE_BATCH_SIZE
from app.core.errors import ConflictError
from app.core.http.deps import DbSession, Jobs
from app.core.jobs.periodic import heartbeat
from app.core.jobs.queue import JobQueue, Payload
from app.core.jobs.tasks import RETRY, resources_of, settings_of
from app.core.logging import configure_logging
from app.core.resources import Resources
from app.core.sentry import configure_sentry
from tests.factories import make_audit_event, make_user
from tests.jobs import (
    Interrupt,
    RenameUser,
    RollbackError,
    RunJobs,
    SendDigest,
    UserNamed,
    add_aged_jobs,
    defer_and_roll_back,
    defer_raw,
    deferred_jobs,
    job_ages,
    job_ids,
    job_row,
    mark_running,
    postpone,
    publish_and_roll_back,
    set_attempts,
)
from tests.logs import json_lines
from tests.sentry import FAKE_DSN, CapturingTransport, capture_sentry
from tests.statements import record_deletions, record_statements

NAMING = JobQueue(
    job_app, {UserNamed.event_type: ("tests.apply_name", "tests.ignore_name")}
)

router = APIRouter()


class Unregistered(Payload):
    task_name: ClassVar[str] = "tests.unregistered"


class RenameRefusedError(ConflictError):
    code = "tests.rename_refused"


@router.post("/users/{user_id}/renames", status_code=202)
async def rename(user_id: uuid.UUID, session: DbSession, jobs: Jobs) -> None:
    await jobs.defer(session, RenameUser(user_id=user_id, name="Ada Lovelace"))


@router.post("/users/{user_id}/refused-renames")
async def refuse_rename(user_id: uuid.UUID, session: DbSession, jobs: Jobs) -> None:
    await jobs.defer(session, RenameUser(user_id=user_id, name="Grace Hopper"))
    raise RenameRefusedError


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


@pytest.fixture
def sentry(settings: Settings) -> CapturingTransport:
    configure_sentry(settings.model_copy(update={"sentry_dsn": SecretStr(FAKE_DSN)}))
    return capture_sentry()


@pytest.fixture
def structured_logs(settings: Settings) -> None:
    configure_logging(settings)


async def test_jobs_run_in_the_test_transaction(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    job_id = await resources.jobs.defer(session, payload)

    await run_jobs()

    await session.refresh(ada)
    row = await job_row(session, job_id)
    assert ada.name == "Ada Lovelace"
    assert (row.status, row.attempts) == ("succeeded", 1)
    assert row.args == {"user_id": str(ada.id), "name": "Ada Lovelace"}


async def test_rolled_back_jobs_never_run(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
    run_jobs: RunJobs,
) -> None:
    ada = await make_user(session, name="Ada")

    payload = RenameUser(user_id=ada.id, name="Grace Hopper")

    with pytest.raises(RollbackError):
        await defer_and_roll_back(sessionmaker, resources.jobs, payload)
    await run_jobs()

    await session.refresh(ada)
    assert ada.name == "Ada"
    assert await job_ids(session, RenameUser.task_name) == []


async def test_requests_defer_jobs_with_their_transaction(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")

    response = await client.post(f"/users/{ada.id}/renames")
    await run_jobs()

    await session.refresh(ada)
    assert (response.status_code, ada.name) == (202, "Ada Lovelace")


async def test_a_failed_request_drops_its_jobs(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")

    response = await client.post(f"/users/{ada.id}/refused-renames")
    await run_jobs()

    await session.refresh(ada)
    assert (response.status_code, ada.name) == (409, "Ada")
    assert await job_ids(session, RenameUser.task_name) == []


async def test_jobs_with_unknown_fields_still_run(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")
    await defer_raw(
        session,
        RenameUser.task_name,
        user_id=str(ada.id),
        name="Ada Lovelace",
        title="Countess",
    )

    await run_jobs()

    await session.refresh(ada)
    assert ada.name == "Ada Lovelace"


async def test_payloads_must_name_a_registered_task(
    session: AsyncSession, resources: Resources
) -> None:
    with pytest.raises(TaskNotFound):
        await resources.jobs.defer(session, Unregistered())


async def test_a_refused_defer_leaves_the_transaction_usable(
    session: AsyncSession, resources: Resources
) -> None:
    ada = await make_user(session, name="Ada")
    first = await resources.jobs.defer(session, SendDigest())

    with pytest.raises(AlreadyEnqueued):
        await resources.jobs.defer(session, SendDigest())
    ada.name = "Ada Lovelace"
    await session.flush()

    assert await job_ids(session, SendDigest.task_name) == [first]


async def test_events_defer_one_job_per_subscriber(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")
    event = UserNamed(user_id=ada.id, name="Ada Lovelace")

    await NAMING.publish(session, event)
    await run_jobs()

    await session.refresh(ada)
    fields = {"user_id": str(ada.id), "name": "Ada Lovelace"}
    assert await deferred_jobs(session) == [
        ("tests.apply_name", fields),
        ("tests.ignore_name", fields),
    ]
    assert ada.name == "Ada Lovelace"


async def test_rolled_back_events_publish_nothing(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_jobs: RunJobs,
) -> None:
    ada = await make_user(session, name="Ada")
    event = UserNamed(user_id=ada.id, name="Grace Hopper")

    with pytest.raises(RollbackError):
        await publish_and_roll_back(sessionmaker, NAMING, event)
    await run_jobs()

    await session.refresh(ada)
    assert ada.name == "Ada"
    assert await deferred_jobs(session) == []


async def test_events_without_subscribers_defer_nothing(session: AsyncSession) -> None:
    event = UserNamed(user_id=uuid.uuid7(), name="Ada Lovelace")

    await JobQueue(job_app, {}).publish(session, event)

    assert await deferred_jobs(session) == []


async def test_subscribers_must_be_registered_tasks(session: AsyncSession) -> None:
    queue = JobQueue(job_app, {UserNamed.event_type: ("tests.unregistered",)})
    event = UserNamed(user_id=uuid.uuid7(), name="Ada Lovelace")

    with pytest.raises(TaskNotFound):
        await queue.publish(session, event)


async def test_failed_jobs_retry_two_seconds_later(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    payload = RenameUser(user_id=uuid.uuid7(), name="Nobody")
    job_id = await resources.jobs.defer(session, payload)
    before = datetime.now(UTC)

    with pytest.raises(NoResultFound):
        await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("todo", 1)
    assert row.scheduled_at is not None
    assert timedelta(seconds=2) <= row.scheduled_at - before < timedelta(seconds=3)
    assert sentry.events() == []


async def test_final_failures_reach_sentry_tagged_with_the_job(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    payload = RenameUser(user_id=uuid.uuid7(), name="Nobody")
    job_id = await resources.jobs.defer(session, payload)
    await set_attempts(session, job_id, RETRY.max_attempts - 1)

    with pytest.raises(NoResultFound):
        await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("failed", RETRY.max_attempts)
    [event] = sentry.events()
    assert event["exception"]["values"][0]["type"] == "NoResultFound"
    assert event["tags"]["job_id"] == str(job_id)


async def test_job_events_hold_only_their_own_breadcrumbs(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    ada = await make_user(session, name="Ada")
    await resources.jobs.defer(session, RenameUser(user_id=ada.id, name="Ada Lovelace"))
    payload = RenameUser(user_id=uuid.uuid7(), name="Nobody")
    failing = await resources.jobs.defer(session, payload)
    await set_attempts(session, failing, RETRY.max_attempts - 1)

    with pytest.raises(NoResultFound):
        await run_jobs()

    [event] = sentry.events()
    crumbs = [
        crumb["message"]
        for crumb in event["breadcrumbs"]["values"]
        if crumb["category"] == "query"
    ]
    assert any(crumb.startswith("SELECT users.") for crumb in crumbs)
    assert not any(crumb.startswith("UPDATE users") for crumb in crumbs)


@pytest.mark.usefixtures("structured_logs")
async def test_job_logs_carry_the_job_id(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = await make_user(session, name="Ada")
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    job_id = await resources.jobs.defer(session, payload)

    await run_jobs()

    lines = json_lines(caplog)
    renamed = next(line for line in lines if line["event"] == "tests.user_renamed")
    assert renamed["job_id"] == job_id


async def test_stalled_jobs_are_requeued(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    job_id = await resources.jobs.defer(session, payload)
    await mark_running(session, job_id, silent_for=timedelta(hours=1))
    await defer_raw(session, "recover_stalled_jobs", timestamp=0)
    before = datetime.now(UTC)

    await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("todo", 1)
    assert row.scheduled_at is not None
    assert timedelta(seconds=2) <= row.scheduled_at - before < timedelta(seconds=3)


async def test_jobs_of_a_worker_in_its_grace_period_keep_running(
    session: AsyncSession, resources: Resources, run_jobs: RunJobs
) -> None:
    ada = await make_user(session, name="Ada")
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    job_id = await resources.jobs.defer(session, payload)
    await mark_running(session, job_id, silent_for=timedelta(seconds=45))
    recovery = await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("doing", 0)
    assert (await job_row(session, recovery)).status == "succeeded"


async def test_stalled_jobs_out_of_attempts_fail_and_reach_sentry(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    ada = await make_user(session, name="Ada")
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    job_id = await resources.jobs.defer(session, payload)
    await set_attempts(session, job_id, RETRY.max_attempts - 1)
    await mark_running(session, job_id, silent_for=timedelta(hours=1))
    await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("failed", RETRY.max_attempts)
    [event] = sentry.events()
    assert (event["level"], event["tags"]["job_id"]) == ("error", str(job_id))
    assert event["message"] == "Job failed: its worker stopped"


async def test_stalled_jobs_without_retries_fail_and_reach_sentry(
    session: AsyncSession, run_jobs: RunJobs, sentry: CapturingTransport
) -> None:
    job_id = await defer_raw(session, "heartbeat", timestamp=0)
    await mark_running(session, job_id, silent_for=timedelta(hours=1))
    await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("failed", 1)
    [event] = sentry.events()
    assert (event["message"], event["tags"]["job_id"]) == (
        "Job failed: its worker stopped",
        str(job_id),
    )


async def test_recovery_moves_past_jobs_it_cannot_requeue(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    ada = await make_user(session, name="Ada")
    locked = await resources.jobs.defer(session, SendDigest())
    await mark_running(session, locked, silent_for=timedelta(hours=1))
    waiting = await resources.jobs.defer(session, SendDigest())
    await postpone(session, waiting, by=timedelta(hours=1))
    payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
    renaming = await resources.jobs.defer(session, payload)
    await mark_running(session, renaming, silent_for=timedelta(hours=1))
    await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    await run_jobs()

    statuses = [(await job_row(session, job)).status for job in (locked, renaming)]
    assert statuses == ["doing", "todo"]
    [event] = sentry.events()
    assert (event["exception"]["values"][0]["type"], event["tags"]["job_id"]) == (
        "UniqueViolation",
        str(locked),
    )


async def test_pruning_removes_only_audit_events_older_than_a_year(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    now = datetime.now(UTC)
    for days in (730, 400, 300, 0):
        await make_audit_event(session, created_at=now - timedelta(days=days))
    await defer_raw(session, "prune_audit_events", timestamp=0)

    await run_jobs()

    kept = await session.scalars(
        select(AuditEvent.created_at).order_by(AuditEvent.created_at)
    )
    assert [(now - created_at).days for created_at in kept] == [300, 0]


async def test_pruning_removes_only_jobs_finished_over_a_week_ago(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    month = timedelta(days=30)
    for status in ("succeeded", "failed", "cancelled", "aborted"):
        await add_aged_jobs(session, status, age=month)
    await add_aged_jobs(session, "succeeded", age=timedelta(days=10))
    await add_aged_jobs(session, "succeeded", age=timedelta(days=3))
    await add_aged_jobs(session, "todo", age=month)
    await add_aged_jobs(session, "doing", age=month)
    prune = await defer_raw(session, "prune_finished_jobs", timestamp=0)

    await run_jobs()

    assert await job_ages(session, "heartbeat") == [
        ("succeeded", 3),
        ("todo", 30),
        ("doing", 30),
    ]
    assert (await job_row(session, prune)).status == "succeeded"


async def test_finished_jobs_go_a_batch_at_a_time(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    month = timedelta(days=30)
    await add_aged_jobs(session, "succeeded", age=month, count=DELETE_BATCH_SIZE + 1)
    await defer_raw(session, "prune_finished_jobs", timestamp=0)
    deletions = await record_deletions(session)

    await run_jobs()

    remaining = await job_ages(session, "heartbeat")
    assert (deletions, remaining) == ([DELETE_BATCH_SIZE, 1], [])


async def test_each_batch_of_jobs_commits_on_its_own(
    session: AsyncSession, run_jobs: RunJobs
) -> None:
    month = timedelta(days=30)
    await add_aged_jobs(session, "succeeded", age=month, count=DELETE_BATCH_SIZE + 1)
    await defer_raw(session, "prune_finished_jobs", timestamp=0)
    statements = await record_statements(session)

    await run_jobs()

    assert [statement.split()[0] for statement in statements] == [
        *["SAVEPOINT", "SELECT", "DELETE", "RELEASE"] * 2,
        "SAVEPOINT",
        "SELECT",
        "RELEASE",
    ]


def test_periodic_tasks_keep_their_schedules_and_queueing_locks() -> None:
    schedules = sorted(
        (scheduled.task.name, scheduled.cron, scheduled.task.queueing_lock)
        for scheduled in job_app.periodic_registry.periodic_tasks.values()
    )

    assert schedules == [
        ("auth.prune_expired_rows", "0 * * * *", "auth.prune_expired_rows"),
        ("heartbeat", "*/5 * * * *", None),
        ("prune_audit_events", "0 * * * *", "prune_audit_events"),
        ("prune_finished_jobs", "0 * * * *", "prune_finished_jobs"),
        ("recover_stalled_jobs", "*/5 * * * *", "recover_stalled_jobs"),
    ]


@pytest.mark.usefixtures("structured_logs")
async def test_heartbeat_and_recovery_run_ahead_of_queued_jobs(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = await make_user(session, name="Ada")
    await resources.jobs.defer(session, RenameUser(user_id=ada.id, name="Ada Lovelace"))
    await defer_raw(session, "heartbeat", timestamp=0)
    await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    await run_jobs()

    started = [
        line["job"]["task_name"]
        for line in json_lines(caplog)
        if line.get("action") == "start_job"
    ]
    assert started == ["heartbeat", "recover_stalled_jobs", RenameUser.task_name]


async def test_jobs_cancelled_on_their_last_attempt_reach_sentry(
    session: AsyncSession,
    resources: Resources,
    run_jobs: RunJobs,
    sentry: CapturingTransport,
) -> None:
    job_id = await resources.jobs.defer(session, Interrupt())
    await set_attempts(session, job_id, RETRY.max_attempts - 1)

    with pytest.raises(asyncio.CancelledError):
        await run_jobs()

    row = await job_row(session, job_id)
    assert (row.status, row.attempts) == ("aborted", RETRY.max_attempts)
    [event] = sentry.events()
    assert (event["exception"]["values"][0]["type"], event["tags"]["job_id"]) == (
        "CancelledError",
        str(job_id),
    )


async def test_the_heartbeat_checks_in_with_sentry(sentry: CapturingTransport) -> None:
    await heartbeat(timestamp=0)

    [check_in] = sentry.items("check_in")
    assert (check_in["monitor_slug"], check_in["status"]) == ("worker-heartbeat", "ok")
    assert check_in["monitor_config"]["schedule"] == {
        "type": "crontab",
        "value": "*/5 * * * *",
    }


def test_retries_double_from_two_seconds_up_to_an_hour() -> None:
    delays = [RETRY.delay(attempts) for attempts in range(RETRY.max_attempts)]

    assert delays == [
        *(timedelta(seconds=2**n) for n in range(1, 12)),
        timedelta(hours=1),
        timedelta(hours=1),
        None,
    ]


@pytest.mark.parametrize(
    ("read", "missing"),
    [(settings_of, "Settings"), (resources_of, "Resources")],
    ids=["settings", "resources"],
)
def test_jobs_need_the_worker_to_carry_settings_and_resources(
    read: Callable[[JobContext], object], missing: str
) -> None:
    job = Job(id=1, queue="default", lock=None, queueing_lock=None, task_name="x")
    context = JobContext(
        app=job_app, job=job, start_timestamp=0.0, abort_reason=lambda: None
    )

    with pytest.raises(TypeError, match=f"without {missing}"):
        read(context)
