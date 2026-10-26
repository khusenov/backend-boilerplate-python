import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import URL, Engine, Pool, event
from sqlalchemy.exc import ArgumentError
from starlette.types import Message

from app.core.config import Settings
from app.core.db import psycopg_url
from app.main import create_app
from tests.clients import serve


class ServerStoppedError(Exception):
    pass


async def test_shutdown_disposes_the_engine(settings: Settings) -> None:
    disposed: list[URL] = []
    opened: list[object] = []
    closed: list[object] = []

    def record_disposal(engine: Engine) -> None:
        disposed.append(engine.url)

    def record_open(connection: object, _: object) -> None:
        opened.append(connection)

    def record_close(connection: object, _: object) -> None:
        closed.append(connection)

    event.listen(Engine, "engine_disposed", record_disposal)
    event.listen(Pool, "connect", record_open)
    event.listen(Pool, "close", record_close)
    try:
        async with serve(create_app(settings)) as client:
            assert (await client.get("/health/ready")).status_code == 200
            assert (disposed, closed) == ([], [])
    finally:
        event.remove(Pool, "close", record_close)
        event.remove(Pool, "connect", record_open)
        event.remove(Engine, "engine_disposed", record_disposal)

    assert disposed == [psycopg_url(settings.database_url)]
    assert closed == opened


async def test_a_server_stopping_on_an_error_disposes_the_engine(
    settings: Settings,
) -> None:
    disposed: list[URL] = []

    def record(engine: Engine) -> None:
        disposed.append(engine.url)

    event.listen(Engine, "engine_disposed", record)
    try:
        with pytest.raises(ServerStoppedError):
            async with LifespanManager(create_app(settings)):
                raise ServerStoppedError
    finally:
        event.remove(Engine, "engine_disposed", record)

    assert disposed == [psycopg_url(settings.database_url)]


async def test_startup_failures_reach_the_server(settings: Settings) -> None:
    update = {"database_url": SecretStr("not a database url")}
    app = create_app(settings.model_copy(update=update))
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    with pytest.raises(ArgumentError, match="Could not parse SQLAlchemy URL"):
        await app({"type": "lifespan", "state": {}}, receive, send)

    assert [message["type"] for message in sent] == ["lifespan.startup.failed"]
