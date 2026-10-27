import asyncio
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, cast

import psycopg
import pytest
import sentry_sdk
from alembic import command
from fastapi import FastAPI
from httpx import AsyncClient
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from app.bootstrap import job_app
from app.core.config import Settings
from app.core.jobs.queue import JobQueue
from app.core.resources import Resources
from app.main import create_app
from app.modules.auth import public as auth
from tests import jobs
from tests.clients import serve
from tests.migrations import alembic_config
from tests.scratch import ScratchBase
from tests.sentry import FAKE_DSN, CapturingTransport


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        help="rewrite the files in tests/architecture/snapshots",
    )


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:18", driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres: PostgresContainer) -> str:
    url = postgres.get_connection_url()
    with alembic_config(url) as config:
        command.upgrade(config, "head")
    return url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


def is_committed(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("committed") is not None


async def truncate_tables(connection: AsyncConnection) -> None:
    tables = await connection.scalars(
        text(
            "SELECT quote_ident(tablename) FROM pg_tables "
            "WHERE schemaname = current_schema() AND tablename <> 'alembic_version'"
        )
    )
    await connection.execute(text(f"TRUNCATE {', '.join(tables)}"))
    await connection.commit()


@pytest.fixture
async def connection(
    request: pytest.FixtureRequest, engine: AsyncEngine
) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
        if is_committed(request):
            await truncate_tables(connection)


@pytest.fixture
def sessionmaker(
    request: pytest.FixtureRequest, engine: AsyncEngine, connection: AsyncConnection
) -> async_sessionmaker[AsyncSession]:
    if is_committed(request):
        return async_sessionmaker(engine, expire_on_commit=False)
    return async_sessionmaker(
        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session


@pytest.fixture
async def scratch_tables(connection: AsyncConnection) -> None:
    await connection.run_sync(ScratchBase.metadata.create_all)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        app_env="test",
        secret_key=SecretStr("test-secret-key"),
        database_url=SecretStr(database_url),
        cors_origins=("https://app.example.com",),
        trusted_proxy_hops=1,
    )


@pytest.fixture
def production_settings(database_url: str) -> Settings:
    return Settings(
        app_env="production",
        secret_key=SecretStr("x" * 32),
        database_url=SecretStr(database_url),
        cors_origins=("https://app.example.com",),
        trusted_proxy_hops=1,
        sentry_dsn=SecretStr(FAKE_DSN),
        sentry_environment="production",
    )


@pytest.fixture(autouse=True)
def offline_sentry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("sentry_sdk.transport.HttpTransport", CapturingTransport)
    yield
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_current_scope().clear()


@pytest.fixture
def resources(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> Iterator[Resources]:
    with ThreadPoolExecutor(4) as hashing:
        yield Resources(
            engine=engine,
            sessionmaker=sessionmaker,
            jobs=JobQueue(job_app),
            limiter=MovingWindowRateLimiter(MemoryStorage()),
            hashing=hashing,
            hashing_slots=asyncio.Semaphore(4),
            authenticate=auth.authenticate,
        )


@pytest.fixture
async def run_jobs(connection: AsyncConnection, resources: Resources) -> jobs.RunJobs:
    raw = await connection.get_raw_connection()
    driver = cast(psycopg.AsyncConnection[Any], raw.driver_connection)
    return partial(jobs.run_jobs, driver, resources)


@pytest.fixture
def app(settings: Settings, resources: Resources) -> FastAPI:
    return create_app(settings, resources)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app) as client:
        yield client
