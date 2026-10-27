import asyncio
import re
import signal
import sys
import uuid
from asyncio import StreamReader
from asyncio.subprocess import PIPE, STDOUT
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.jobs.tasks import RETRY
from app.core.resources import Resources
from app.core.sentry import configure_sentry
from app.modules.users.models import User
from app.worker import work
from tests.factories import make_user
from tests.jobs import (
    FinishSoon,
    Hang,
    Meet,
    RecordTimeouts,
    RenameUser,
    RollbackError,
    defer_and_roll_back,
    defer_raw,
    job_ids,
    job_row,
    mark_running,
    meeting,
    set_attempts,
)
from tests.sentry import FAKE_DSN, capture_sentry

HEARTBEAT_DONE = re.compile(
    r"Job heartbeat\[\d+\]\(timestamp=\d+\) ended with status: Success"
)


async def finished_status(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: int
) -> str:
    async with asyncio.timeout(10):
        while True:
            async with sessionmaker() as session:
                status = (await job_row(session, job_id)).status
            if status not in {"todo", "doing"}:
                return status
            await asyncio.sleep(0.1)


async def wait_until_running(
    sessionmaker: async_sessionmaker[AsyncSession], *jobs: int
) -> None:
    async with asyncio.timeout(10):
        while True:
            async with sessionmaker() as session:
                statuses = {(await job_row(session, job_id)).status for job_id in jobs}
            if statuses == {"doing"}:
                return
            await asyncio.sleep(0.1)


async def work_until_finished(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession], *jobs: int
) -> list[str]:
    async with asyncio.TaskGroup() as group:
        worker = group.create_task(work(settings))
        statuses = [await finished_status(sessionmaker, job_id) for job_id in jobs]
        worker.cancel()
    return statuses


async def read_until(stream: StreamReader, pattern: re.Pattern[str]) -> str:
    lines: list[str] = []
    async for line in stream:
        lines.append(line.decode())
        if pattern.search(lines[-1]):
            break
    return "".join(lines)


@pytest.mark.committed
async def test_the_worker_runs_committed_jobs_only(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        ada = await make_user(session, name="Ada")
    lost = RenameUser(user_id=ada.id, name="Grace Hopper")
    with pytest.raises(RollbackError):
        await defer_and_roll_back(sessionmaker, resources.jobs, lost)
    async with sessionmaker.begin() as session:
        payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
        job_id = await resources.jobs.defer(session, payload)

    statuses = await work_until_finished(settings, sessionmaker, job_id)

    async with sessionmaker() as session:
        renamed = await session.get_one(User, ada.id)
        renames = await job_ids(session, RenameUser.task_name)
    assert (statuses, renamed.name, renames) == (
        ["succeeded"],
        "Ada Lovelace",
        [job_id],
    )


@pytest.mark.committed
async def test_the_worker_reports_final_failures_tagged_with_their_job(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    reporting = settings.model_copy(update={"sentry_dsn": SecretStr(FAKE_DSN)})
    configure_sentry(reporting)
    sentry = capture_sentry()
    async with sessionmaker.begin() as session:
        payload = RenameUser(user_id=uuid.uuid7(), name="Nobody")
        failing = await resources.jobs.defer(session, payload)
        await set_attempts(session, failing, RETRY.max_attempts - 1)
        unknown = await defer_raw(session, "tests.removed", allow_unknown=True)

    statuses = await work_until_finished(reporting, sessionmaker, failing, unknown)

    reports = {
        event["exception"]["values"][0]["type"]: event["tags"]["job_id"]
        for event in sentry.events()
    }
    assert statuses == ["failed", "failed"]
    assert reports == {"NoResultFound": str(failing), "TaskNotFound": str(unknown)}
    assert len(sentry.events()) == 2


@pytest.mark.committed
async def test_worker_connections_allow_a_minute_per_statement(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        ada = await make_user(session, name="Ada")
        job_id = await resources.jobs.defer(session, RecordTimeouts(user_id=ada.id))

    statuses = await work_until_finished(settings, sessionmaker, job_id)

    async with sessionmaker() as session:
        recorded = await session.get_one(User, ada.id)
    assert (statuses, recorded.name) == (["succeeded"], "1min 30s, 1min 30s")


@pytest.mark.committed
async def test_the_worker_runs_ten_jobs_at_once(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        meetings = [
            await resources.jobs.defer(session, Meet()) for _ in range(meeting.parties)
        ]

    statuses = await work_until_finished(settings, sessionmaker, *meetings)

    assert statuses == ["succeeded"] * 10


@pytest.mark.committed
async def test_starting_workers_leave_a_stopping_worker_its_jobs(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        ada = await make_user(session, name="Ada")
        payload = RenameUser(user_id=ada.id, name="Ada Lovelace")
        running = await resources.jobs.defer(session, payload)
        await mark_running(session, running, silent_for=timedelta(seconds=45))
        recovery = await defer_raw(session, "recover_stalled_jobs", timestamp=0)

    statuses = await work_until_finished(settings, sessionmaker, recovery)

    async with sessionmaker() as session:
        row = await job_row(session, running)
    assert (statuses, row.status, row.attempts) == (["succeeded"], "doing", 0)


@pytest.mark.committed
async def test_stopping_workers_give_jobs_30_seconds_then_retry_the_rest(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    async with sessionmaker.begin() as session:
        soon = await resources.jobs.defer(session, FinishSoon())
        hung = await resources.jobs.defer(session, Hang())
    clock = asyncio.get_running_loop()

    async with asyncio.timeout(45):
        async with asyncio.TaskGroup() as group:
            worker = group.create_task(work(settings))
            await wait_until_running(sessionmaker, soon, hung)
            worker.cancel()
            stopped_at = clock.time()
        waited = clock.time() - stopped_at

    async with sessionmaker() as session:
        done = await job_row(session, soon)
        retried = await job_row(session, hung)
    assert (done.status, retried.status, retried.attempts) == ("succeeded", "todo", 1)
    assert waited >= 30


@pytest.mark.committed
@pytest.mark.usefixtures("connection", "environment")
async def test_the_worker_process_beats_until_sigterm() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.worker", stdout=PIPE, stderr=STDOUT
    )
    assert process.stdout is not None

    try:
        async with asyncio.timeout(30):
            started = await read_until(process.stdout, HEARTBEAT_DONE)
            assert HEARTBEAT_DONE.search(started), started
            process.send_signal(signal.SIGTERM)
            stopped = (await process.stdout.read()).decode()
            code = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert (code, "Traceback" in started + stopped) == (0, False)
    assert "Stopped worker" in stopped
