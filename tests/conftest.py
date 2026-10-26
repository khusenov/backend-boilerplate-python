from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from app.core.config import Settings
from app.core.resources import Resources
from app.main import create_app
from tests.clients import serve
from tests.migrations import alembic_config
from tests.scratch import ScratchBase


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


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()


@pytest.fixture
def sessionmaker(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def scratch_tables(connection: AsyncConnection) -> None:
    await connection.run_sync(ScratchBase.metadata.create_all)


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
    )


@pytest.fixture
def resources(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> Resources:
    return Resources(engine=engine, sessionmaker=sessionmaker)


@pytest.fixture
def app(settings: Settings, resources: Resources) -> FastAPI:
    return create_app(settings, resources)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app) as client:
        yield client
