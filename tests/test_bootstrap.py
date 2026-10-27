import asyncio
import threading
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.bootstrap import build_resources
from app.core.config import Settings
from app.core.resources import Resources
from app.modules.auth import public as auth
from tests.scratch import ScratchNote


@pytest.fixture
async def built_resources(settings: Settings) -> AsyncIterator[Resources]:
    resources = build_resources(settings)
    yield resources
    await resources.close()


async def test_sessions_time_out_statements_and_idle_transactions(
    built_resources: Resources,
) -> None:
    async with built_resources.sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT current_setting('statement_timeout'), "
                "current_setting('idle_in_transaction_session_timeout')"
            )
        )

    assert tuple(result.one()) == ("5s", "30s")


async def test_connects_time_out(built_resources: Resources) -> None:
    async with built_resources.engine.connect() as connection:
        raw = await connection.get_raw_connection()
        assert raw.driver_connection is not None
        parameters = raw.driver_connection.info.get_parameters()

    assert parameters["connect_timeout"] == "5"


async def test_errors_hide_parameters(built_resources: Resources) -> None:
    async with built_resources.engine.connect() as connection:
        with pytest.raises(DBAPIError, match="division by zero") as caught:
            await connection.execute(
                text("SELECT length(:email) / 0"), {"email": "ada@example.com"}
            )

    assert "SQL parameters hidden" in str(caught.value)
    assert "ada@example.com" not in str(caught.value)


async def test_dropped_connections_are_replaced(
    built_resources: Resources, engine: AsyncEngine
) -> None:
    async with built_resources.engine.connect() as connection:
        dropped = await connection.scalar(text("SELECT pg_backend_pid()"))
    async with engine.connect() as admin:
        await admin.execute(
            text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": dropped}
        )

    async with built_resources.engine.connect() as connection:
        replacement = await connection.scalar(text("SELECT pg_backend_pid()"))

    assert replacement != dropped


@pytest.mark.usefixtures("scratch_tables")
async def test_committed_objects_stay_loaded(
    built_resources: Resources, connection: AsyncConnection
) -> None:
    session = built_resources.sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    async with session, session.begin():
        note = ScratchNote(body="kept")
        session.add(note)

    assert note.body == "kept"


async def test_passwords_hash_four_at_a_time(built_resources: Resources) -> None:
    release = threading.Event()
    threads: list[str] = []

    def hold() -> None:
        threads.append(threading.current_thread().name)
        release.wait(timeout=5)

    loop = asyncio.get_running_loop()
    jobs = [loop.run_in_executor(built_resources.hashing, hold) for _ in range(8)]
    await asyncio.sleep(0.2)
    started = len(threads)
    release.set()
    await asyncio.gather(*jobs)

    assert started == 4
    assert sorted(set(threads)) == [f"hashing_{n}" for n in range(4)]


async def test_logins_get_four_hashing_slots(built_resources: Resources) -> None:
    for _ in range(4):
        await built_resources.hashing_slots.acquire()

    assert built_resources.hashing_slots.locked()


async def test_close_stops_the_hashing_threads(built_resources: Resources) -> None:
    await built_resources.close()

    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        built_resources.hashing.submit(print)


def test_principals_come_from_auth(built_resources: Resources) -> None:
    assert built_resources.authenticate is auth.authenticate
