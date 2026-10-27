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
from app.core.jobs.queue import JobQueue, Message, driver_connection
from app.core.jobs.tasks import observe_job, resources_of, task
from app.core.resources import Resources
from app.modules.users.models import User

type RunJobs = Callable[[], Awaitable[None]]

TIMEOUTS = (
    "SELECT current_setting('statement_timeout') || ' ' "
    "|| current_setting('idle_in_transaction_session_timeout') AS timeouts"
)

logger = structlog.stdlib.get_logger(__name__)
blueprint = Blueprint()
meeting = asyncio.Barrier(10)


class RenameUser(Message):
    task_name: ClassVar[str] = "tests.rename_user"

    user_id: uuid.UUID
    name: str


class SendDigest(Message):
    task_name: ClassVar[str] = "tests.send_digest"


class RecordTimeouts(Message):
    task_name: ClassVar[str] = "tests.record_timeouts"

    user_id: uuid.UUID


class Meet(Message):
    task_name: ClassVar[str] = "tests.meet"


class Interrupt(Message):
    task_name: ClassVar[str] = "tests.interrupt"


class FinishSoon(Message):
    task_name: ClassVar[str] = "tests.finish_soon"


class Hang(Message):
    task_name: ClassVar[str] = "tests.hang"


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
    connection: psycopg.AsyncConnection[Any], resources: Resources
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
                additional_context={"resources": resources},
                worker_middleware=[observe_job, reraise],
            )
    finally:
        job_app.periodic_registry = periodic_registry
    if errors:
        raise errors[0]


class RollbackError(Exception):
    pass


async def defer_and_roll_back(
    sessionmaker: async_sessionmaker[AsyncSession], jobs: JobQueue, payload: Message
) -> None:
    async with sessionmaker.begin() as session:
        await jobs.defer(session, payload)
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
