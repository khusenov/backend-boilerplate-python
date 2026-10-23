import asyncio
import logging
import uuid

import pytest
import structlog
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient

from tests.logs import json_lines

router = APIRouter()


@router.post("/sessions")
async def sign_in() -> None:
    structlog.contextvars.bind_contextvars(user_id="user-1")


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("boom")


@router.get("/slow")
async def slow() -> None:
    await asyncio.sleep(0.02)


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


async def test_incoming_request_id_is_ignored(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    forged = str(uuid.uuid7())

    response = await client.get("/missing", headers={"X-Request-ID": forged})

    (access,) = [line for line in json_lines(caplog) if line["event"] == "http.request"]
    assert access["request_id"] == response.headers["x-request-id"]
    assert access["request_id"] != forged
    assert uuid.UUID(access["request_id"]).version == 7


async def test_missing_request_id_is_generated(client: AsyncClient) -> None:
    responses = [await client.get("/missing"), await client.get("/missing")]

    request_ids = {response.headers["x-request-id"] for response in responses}
    assert len(request_ids) == 2
    assert all(uuid.UUID(request_id).version == 7 for request_id in request_ids)


async def test_one_access_line_per_request_outside_health(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await client.get("/health/live")
    responses = [await client.get("/healthcheck"), await client.post("/sessions")]

    access = [line for line in json_lines(caplog) if line["event"] == "http.request"]
    assert [(line["method"], line["path"], line["status"]) for line in access] == [
        ("GET", "/healthcheck", 404),
        ("POST", "/sessions", 200),
    ]
    assert [line["request_id"] for line in access] == [
        response.headers["x-request-id"] for response in responses
    ]


async def test_access_line_duration_is_in_milliseconds(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await client.get("/slow")

    (access,) = [line for line in json_lines(caplog) if line["event"] == "http.request"]
    assert access["duration_ms"] >= 10


async def test_failed_request_is_logged_as_500(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(RuntimeError) as caught:
        await client.get("/boom")
    logging.getLogger("uvicorn.error").error(
        "Exception in ASGI application\n", exc_info=caught.value
    )

    access, server_error = json_lines(caplog)
    assert (access["event"], access["status"]) == ("http.request", 500)
    assert server_error["request_id"] == access["request_id"]
    assert server_error["exception"].endswith("RuntimeError: boom")


async def test_log_bindings_last_for_one_request(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await client.post("/sessions")
    await client.get("/missing")

    access = [line for line in json_lines(caplog) if line["event"] == "http.request"]
    assert [line.get("user_id") for line in access] == ["user-1", None]
