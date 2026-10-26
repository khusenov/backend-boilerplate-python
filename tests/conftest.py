from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from testcontainers.community.postgres import PostgresContainer

from app.core.config import Settings
from app.main import create_app
from tests.migrations import alembic_config


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
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            yield client
