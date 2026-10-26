import asyncio
import io
import socket
import time
from collections.abc import AsyncIterator
from contextlib import redirect_stdout

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.main import create_app
from tests.clients import serve


def app_with_database_url(settings: Settings, database_url: str) -> FastAPI:
    update = {"database_url": SecretStr(database_url)}
    return create_app(settings.model_copy(update=update))


@pytest.fixture
def refused_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"postgresql://app:app@127.0.0.1:{port}/app"


@pytest.fixture
async def silent_url() -> AsyncIterator[str]:
    async def ignore(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(ignore, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"postgresql://app:app@127.0.0.1:{port}/app"
        server.close_clients()


async def test_live_answers_without_the_database(
    settings: Settings, refused_url: str
) -> None:
    async with serve(app_with_database_url(settings, refused_url)) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


async def test_ready_passes_with_the_database(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    statements: list[str] = []

    def record(statement: str, **_: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record, named=True)
    try:
        response = await client.get("/health/ready")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert statements == ["SELECT 1"]


async def test_ready_fails_without_the_database(
    settings: Settings, refused_url: str
) -> None:
    with redirect_stdout(io.StringIO()) as output:
        async with serve(app_with_database_url(settings, refused_url)) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "code": "service_unavailable",
        "request_id": response.headers["x-request-id"],
    }
    assert "health.not_ready" in output.getvalue()
    assert "Connection refused" in output.getvalue()


async def test_ready_gives_up_after_a_second(
    settings: Settings, silent_url: str
) -> None:
    async with serve(app_with_database_url(settings, silent_url)) as client:
        started = time.perf_counter()
        response = await client.get("/health/ready")
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert 1 <= elapsed < 1.5
