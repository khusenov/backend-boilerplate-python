import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, ClassVar, NamedTuple, cast

import psycopg
import structlog
from procrastinate import Blueprint, JobContext, PsycopgConnector
from procrastinate.middleware import AsyncCallNext
from procrastinate.periodic import PeriodicRegistry
from procrastinate.worker import Worker
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap import job_app
from app.core.config import Settings
from app.core.jobs.queue import Event, JobQueue, Payload, driver_connection
from app.core.jobs.tasks import observe_job, resources_of, settings_of, subscriber, task
from app.core.resources import Resources
from app.modules.users.models import User
from app.worker import work

type RunJobs = Callable[[], Awaitable[None]]

TIMEOUTS = (
    "SELECT current_setting('statement_timeout') || ' ' "
    "|| current_setting('idle_in_transaction_session_timeout') AS timeouts"
)

logger = structlog.stdlib.get_logger(__name__)
blueprint = Blueprint()
meeting = asyncio.Barrier(10)


class RenameUser(Payload):
    task_name: ClassVar[str] = "tests.rename_user"

    user_id: uuid.UUID
    name: str


class SendDigest(Payload):
    task_name: ClassVar[str] = "tests.send_digest"


class RecordTimeouts(Payload):
    task_name: ClassVar[str] = "tests.record_timeouts"

    user_id: uuid.UUID


class RecordEnvironment(Payload):
    task_name: ClassVar[str] = "tests.record_environment"

    user_id: uuid.UUID


class Meet(Payload):
    task_name: ClassVar[str] = "tests.meet"


class Interrupt(Payload):
    task_name: ClassVar[str] = "tests.interrupt"


class FinishSoon(Payload):
    task_name: ClassVar[str] = "tests.finish_soon"


class Hang(Payload):
    task_name: ClassVar[str] = "tests.hang"


class UserNamed(Event):
    event_type: ClassVar[str] = "tests.user_named.v1"

    user_id: uuid.UUID
    name: str


@task(blueprint, RenameUser)
async def rename_user(context: JobContext, payload: RenameUser) -> None:
    async with resources_of(context).sessionmaker.begin() as session:
        user = await session.get_one(User, payload.user_id)
        user.name = payload.name
    logger.info("tests.user_renamed")


@task(blueprint, SendDigest, queueing_lock=SendDigest.task_name)
async def send_digest(context: JobContext, payload: SendDigest) -> None:
    pass


@task(blueprint, RecordTimeouts)
async def record_timeouts(context: JobContext, payload: RecordTimeouts) -> None:
    queue = await context.app.connector.execute_query_one_async(TIMEOUTS)
    async with resources_of(context).sessionmaker.begin() as session:
        user = await session.get_one(User, payload.user_id)
        user.name = f"{await session.scalar(text(TIMEOUTS))}, {queue['timeouts']}"


@task(blueprint, RecordEnvironment)
async def record_environment(context: JobContext, payload: RecordEnvironment) -> None:
    async with resources_of(context).sessionmaker.begin() as session:
        user = await session.get_one(User, payload.user_id)
        user.name = settings_of(context).app_env


@task(blueprint, Meet)
async def meet(context: JobContext, payload: Meet) -> None:
    async with asyncio.timeout(5):
        await meeting.wait()


@task(blueprint, Interrupt)
async def interrupt(context: JobContext, payload: Interrupt) -> None:
    raise asyncio.CancelledError


@task(blueprint, FinishSoon)
async def finish_soon(context: JobContext, payload: FinishSoon) -> None:
    await asyncio.sleep(2)


@task(blueprint, Hang)
async def hang(context: JobContext, payload: Hang) -> None:
    await asyncio.Event().wait()


@subscriber(blueprint, UserNamed, name="tests.apply_name")
async def apply_name(context: JobContext, event: UserNamed) -> None:
    async with resources_of(context).sessionmaker.begin() as session:
        user = await session.get_one(User, event.user_id)
        user.name = event.name


@subscriber(blueprint, UserNamed, name="tests.ignore_name")
async def ignore_name(context: JobContext, event: UserNamed) -> None:
    pass


job_app.add_tasks_from(blueprint, namespace="")


class JobRow(NamedTuple):
    status: str
    attempts: int
    scheduled_at: datetime | None
    args: dict[str, object]


class SingleConnectionPool:
    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any]]:
        await self._connection.execute("SAVEPOINT procrastinate")
        try:
            yield self._connection
        except Exception:
            await self._connection.execute("ROLLBACK TO SAVEPOINT procrastinate")
            raise
        await self._connection.execute("RELEASE SAVEPOINT procrastinate")


async def run_jobs(
    connection: psycopg.AsyncConnection[Any], settings: Settings, resources: Resources
) -> None:
    errors: list[BaseException] = []

    async def reraise(
        call_next: AsyncCallNext, _context: JobContext, _worker: Worker
    ) -> object:
        try:
            return await call_next()
        except BaseException as error:
            errors.append(error)
            raise

    connector = PsycopgConnector()
    pool = cast(AsyncConnectionPool[Any], SingleConnectionPool(connection))
    await connector.open_async(pool=pool)
    periodic_registry = job_app.periodic_registry
    job_app.periodic_registry = PeriodicRegistry()
    try:
        with job_app.replace_connector(connector):
            await job_app.run_worker_async(
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
                concurrency=1,
                stalled_worker_timeout=timedelta(days=1).total_seconds(),
                additional_context={"settings": settings, "resources": resources},
                worker_middleware=[observe_job, reraise],
            )
    finally:
        job_app.periodic_registry = periodic_registry
    if errors:
        raise errors[0]


class RollbackError(Exception):
    pass


async def defer_and_roll_back(
    sessionmaker: async_sessionmaker[AsyncSession], jobs: JobQueue, payload: Payload
) -> None:
    async with sessionmaker.begin() as session:
        await jobs.defer(session, payload)
        raise RollbackError


async def publish_and_roll_back(
    sessionmaker: async_sessionmaker[AsyncSession], jobs: JobQueue, event: Event
) -> None:
    async with sessionmaker.begin() as session:
        await jobs.publish(session, event)
        raise RollbackError


async def defer_raw(
    session: AsyncSession,
    task_name: str,
    /,
    *,
    allow_unknown: bool = False,
    **kwargs: str | int,
) -> int:
    connection = await driver_connection(session)
    deferrer = job_app.configure_task(
        task_name, allow_unknown=allow_unknown, connection=connection
    )
    return await deferrer.defer_async(**kwargs)


async def job_row(session: AsyncSession, job_id: int) -> JobRow:
    result = await session.execute(
        text(
            "SELECT status::text, attempts, scheduled_at, args "
            "FROM procrastinate_jobs WHERE id = :id"
        ),
        {"id": job_id},
    )
    return JobRow(*result.one())


async def deferred_jobs(session: AsyncSession) -> list[tuple[str, dict[str, object]]]:
    result = await session.execute(
        text("SELECT task_name, args FROM procrastinate_jobs ORDER BY id")
    )
    return [(row.task_name, row.args) for row in result]


async def job_ids(session: AsyncSession, task_name: str) -> list[int]:
    result = await session.scalars(
        text("SELECT id FROM procrastinate_jobs WHERE task_name = :name ORDER BY id"),
        {"name": task_name},
    )
    return list(result)


async def set_attempts(session: AsyncSession, job_id: int, attempts: int) -> None:
    await session.execute(
        text("UPDATE procrastinate_jobs SET attempts = :attempts WHERE id = :id"),
        {"id": job_id, "attempts": attempts},
    )


async def postpone(session: AsyncSession, job_id: int, *, by: timedelta) -> None:
    await session.execute(
        text("UPDATE procrastinate_jobs SET scheduled_at = now() + :by WHERE id = :id"),
        {"id": job_id, "by": by},
    )


async def mark_running(
    session: AsyncSession, job_id: int, *, silent_for: timedelta
) -> None:
    await session.execute(
        text(
            "WITH worker AS (INSERT INTO procrastinate_workers (last_heartbeat) "
            "VALUES (now() - :silent_for) RETURNING id) "
            "UPDATE procrastinate_jobs SET status = 'doing', "
            "worker_id = (SELECT id FROM worker) WHERE id = :id"
        ),
        {"id": job_id, "silent_for": silent_for},
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


async def work_until_finished(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession], *ids: int
) -> list[str]:
    async with asyncio.TaskGroup() as group:
        worker = group.create_task(work(settings))
        statuses = [await finished_status(sessionmaker, job_id) for job_id in ids]
        worker.cancel()
    return statuses
