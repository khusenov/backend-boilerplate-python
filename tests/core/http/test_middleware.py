import asyncio
import io
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import redirect_stdout

import pytest
import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.http.middleware import MAX_BODY_BYTES
from app.core.logging import build_formatter
from app.core.schemas import Schema
from app.main import create_app
from tests.logs import json_lines

ORIGIN = "https://app.example.com"
router = APIRouter()


class Note(Schema):
    text: str


@router.post("/sessions")
async def sign_in() -> None:
    structlog.contextvars.bind_contextvars(user_id="user-1")


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("boom")


@router.get("/slow")
async def slow() -> None:
    await asyncio.sleep(0.02)


@router.post("/uploads")
async def upload(request: Request) -> int:
    return len(await request.body())


@router.post("/notes", status_code=201)
async def create_note(body: Note) -> None: ...


@router.get("/cached")
async def cached() -> Response:
    return Response(headers={"Cache-Control": "max-age=60"})


async def stream_of(size: int) -> AsyncIterator[bytes]:
    chunk = 64 * 1024
    for start in range(0, size, chunk):
        yield b"x" * min(chunk, size - start)


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


async def test_console_access_lines_show_the_status_as_a_number(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with redirect_stdout(io.StringIO()):
        console = build_formatter(json_logs=False)

    await client.get("/missing")

    (access,) = [
        record for record in caplog.records if record.name == "app.core.http.middleware"
    ]
    assert "status=404" in console.format(access)


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


async def test_responses_carry_security_headers(client: AsyncClient) -> None:
    response = await client.get("/missing")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in response.headers


async def test_routes_may_set_their_own_cache_control(client: AsyncClient) -> None:
    response = await client.get("/cached")

    assert response.headers["cache-control"] == "max-age=60"


async def test_production_responses_demand_https(
    production_settings: Settings,
) -> None:
    transport = ASGITransport(app=create_app(production_settings))
    async with AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as client:
        response = await client.get("/missing")

    assert (
        response.headers["strict-transport-security"]
        == "max-age=63072000; includeSubDomains"
    )


async def test_allowed_origins_pass_preflight(client: AsyncClient) -> None:
    response = await client.options(
        "/sessions",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert (
        response.headers["access-control-allow-methods"]
        == "GET, POST, PUT, PATCH, DELETE"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in response.headers


async def test_foreign_origins_get_no_cors_headers(client: AsyncClient) -> None:
    origin = {"Origin": "https://evil.example"}
    preflight = await client.options(
        "/sessions", headers=origin | {"Access-Control-Request-Method": "POST"}
    )
    simple = await client.post("/sessions", headers=origin)

    assert (preflight.status_code, simple.status_code) == (400, 200)
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize("streamed", [False, True], ids=["sized", "streamed"])
async def test_body_at_the_limit_is_read(client: AsyncClient, streamed: bool) -> None:
    content = stream_of(MAX_BODY_BYTES) if streamed else b"x" * MAX_BODY_BYTES

    response = await client.post("/uploads", content=content)

    assert ("content-length" not in response.request.headers) is streamed
    assert response.json() == MAX_BODY_BYTES


async def test_declared_oversize_body_is_refused_before_the_route(
    client: AsyncClient,
) -> None:
    response = await client.post("/sessions", content=b"x" * (MAX_BODY_BYTES + 1))

    assert (response.status_code, response.json()["code"]) == (413, "content_too_large")


@pytest.mark.parametrize("path", ["/uploads", "/notes"])
async def test_streamed_oversize_body_is_refused(
    client: AsyncClient, path: str
) -> None:
    response = await client.post(
        path,
        content=stream_of(MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert "content-length" not in response.request.headers
    assert (response.status_code, response.json()["code"]) == (413, "content_too_large")
