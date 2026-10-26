from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        secret_key=SecretStr("test-secret-key"),
        cors_origins=("https://app.example.com",),
        trusted_proxy_hops=1,
    )


@pytest.fixture
def production_settings() -> Settings:
    return Settings(
        app_env="production",
        secret_key=SecretStr("x" * 32),
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
