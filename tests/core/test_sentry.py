import secrets
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path

import pytest
import sentry_sdk
import structlog
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI, HTTPException
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.exc import ArgumentError

from app.core.config import Settings
from app.core.resources import Resources
from app.main import create_app
from tests.clients import serve
from tests.sentry import FAKE_DSN, CapturingTransport, capture_sentry

logger = structlog.stdlib.get_logger(__name__)
router = APIRouter()


@router.post("/boom")
async def boom(body: dict[str, str]) -> None:
    logger.info("note.received", note=body["note"])
    raise RuntimeError("boom")


@router.post("/rejections")
async def reject(body: dict[str, str]) -> None:
    logger.error("note.rejected", note=body["note"])


@router.get("/unavailable")
async def unavailable() -> None:
    raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE)


@pytest.fixture
def reporting_settings(settings: Settings) -> Settings:
    update = {
        "sentry_dsn": SecretStr(FAKE_DSN),
        "sentry_environment": "staging",
        "git_sha": "0123abc",
    }
    return settings.model_copy(update=update)


@pytest.fixture
def app(reporting_settings: Settings, resources: Resources) -> FastAPI:
    app = create_app(reporting_settings, resources)
    app.include_router(router)
    return app


@pytest.fixture
def sentry(app: FastAPI) -> CapturingTransport:
    return capture_sentry()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app, raise_app_exceptions=False) as client:
        yield client


async def test_a_server_error_reaches_sentry_without_the_body(
    client: AsyncClient, sentry: CapturingTransport
) -> None:
    secret = secrets.token_urlsafe(16)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Cookie": f"session={secret}",
        "CF-Connecting-IP": secret,
        "True-Client-IP": secret,
    }

    response = await client.post("/boom", json={"note": secret}, headers=headers)

    assert response.status_code == 500
    (event,) = sentry.events()
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["tags"]["request_id"] == response.headers["x-request-id"]
    assert (event["environment"], event["release"]) == ("staging", "0123abc")
    assert not event["request"].get("data")
    assert secret.encode() not in sentry.payload()


async def test_handled_server_errors_are_reported(
    client: AsyncClient, sentry: CapturingTransport
) -> None:
    response = await client.get("/unavailable")

    assert response.status_code == 503
    (event,) = sentry.events()
    assert event["tags"]["request_id"] == response.headers["x-request-id"]


async def test_client_errors_are_not_reported(
    client: AsyncClient, sentry: CapturingTransport
) -> None:
    missing = await client.get("/missing")
    invalid = await client.post("/boom", json=["not", "an", "object"])

    assert (missing.status_code, invalid.status_code) == (404, 422)
    assert sentry.events() == []


async def test_error_logs_are_not_reported(
    client: AsyncClient, sentry: CapturingTransport
) -> None:
    secret = secrets.token_urlsafe(16)

    response = await client.post("/rejections", json={"note": secret})

    assert response.status_code == 200
    assert sentry.events() == []
    assert secret.encode() not in sentry.payload()


async def test_startup_failures_are_reported_without_a_request_id(
    reporting_settings: Settings,
) -> None:
    update = {"database_url": SecretStr("not a database url")}
    app = create_app(reporting_settings.model_copy(update=update))
    sentry = capture_sentry()

    with pytest.raises(ArgumentError):
        async with LifespanManager(app):
            pass

    (event,) = sentry.events()
    assert event["exception"]["values"][0]["type"] == "ArgumentError"
    assert "request_id" not in event.get("tags", {})


def test_events_default_to_the_app_environment(reporting_settings: Settings) -> None:
    create_app(reporting_settings.model_copy(update={"sentry_environment": None}))
    sentry = capture_sentry()

    sentry_sdk.capture_message("environment probe")

    (event,) = sentry.events()
    assert event["environment"] == "test"


async def test_app_frames_are_in_app_wherever_the_app_is_installed(
    reporting_settings: Settings,
    resources: Resources,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app(reporting_settings, resources)
    app.include_router(router)
    sentry = capture_sentry()

    async with serve(app, raise_app_exceptions=False) as client:
        await client.post("/boom", json={"note": "in-app"})

    (event,) = sentry.events()
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    in_app = {
        frame.get("in_app") for frame in frames if frame["module"].startswith("app.")
    }
    assert in_app == {True}


async def test_shutdown_flushes_sentry(reporting_settings: Settings) -> None:
    app = create_app(reporting_settings)
    sentry = capture_sentry()

    async with serve(app):
        flushes_while_serving = sentry.flushes

    assert (flushes_while_serving, sentry.flushes) == (0, 1)


def test_sentry_stays_off_without_a_dsn(settings: Settings) -> None:
    create_app(settings)

    assert not sentry_sdk.get_client().is_active()
