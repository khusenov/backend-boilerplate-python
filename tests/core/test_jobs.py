import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient
from procrastinate import JobContext
from procrastinate.exceptions import AlreadyEnqueued, TaskNotFound
from procrastinate.jobs import Job
from pydantic import SecretStr
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap import job_app
from app.core.config import Settings
from app.core.errors import ConflictError
from app.core.http.deps import DbSession, Jobs
from app.core.jobs.periodic import heartbeat
from app.core.jobs.queue import Message
from app.core.jobs.tasks import RETRY, resources_of
from app.core.logging import configure_logging
from app.core.resources import Resources
from app.core.sentry import configure_sentry
from tests.factories import make_user
from tests.jobs import (
    Interrupt,
    RenameUser,
    RollbackError,
    RunJobs,
    SendDigest,
    defer_and_roll_back,
    defer_raw,
    job_ids,
    job_row,
    mark_running,
    postpone,
    set_attempts,
)
from tests.logs import json_lines
from tests.sentry import FAKE_DSN, CapturingTransport, capture_sentry

router = APIRouter()


class Unregistered(Message):
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


def test_core_tasks_run_every_five_minutes() -> None:
    schedules = sorted(
        (scheduled.task.name, scheduled.cron)
        for scheduled in job_app.periodic_registry.periodic_tasks.values()
    )

    assert schedules == [
        ("heartbeat", "*/5 * * * *"),
        ("recover_stalled_jobs", "*/5 * * * *"),
    ]


@pytest.mark.usefixtures("structured_logs")
async def test_core_tasks_run_ahead_of_queued_jobs(
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


def test_jobs_need_the_worker_to_carry_resources() -> None:
    job = Job(id=1, queue="default", lock=None, queueing_lock=None, task_name="x")
    context = JobContext(
        app=job_app, job=job, start_timestamp=0.0, abort_reason=lambda: None
    )

    with pytest.raises(TypeError, match="without Resources"):
        resources_of(context)
