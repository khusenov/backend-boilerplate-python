import secrets
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.bootstrap import build_resources
from app.core.config import Settings
from app.core.resources import Resources
from app.main import create_app
from app.modules.auth.service.credentials import PASSWORD_HASHER
from tests.clients import bearer, serve
from tests.factories import (
    make_credential,
    make_login_session,
    make_reset_token,
    make_user,
)
from tests.sentry import FAKE_DSN, CapturingTransport, capture_sentry

REFUSE_CREDENTIAL_WRITES = [
    "CREATE FUNCTION refuse_credential_writes() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN RAISE EXCEPTION 'credential writes are refused'; END $$",
    "CREATE TRIGGER refuse_credential_writes BEFORE INSERT OR UPDATE "
    "ON auth_credentials FOR EACH ROW EXECUTE FUNCTION refuse_credential_writes()",
]


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    built = build_resources(settings)
    yield built.engine
    await built.close()


@pytest.fixture
def app(settings: Settings, resources: Resources) -> FastAPI:
    reporting = settings.model_copy(update={"sentry_dsn": SecretStr(FAKE_DSN)})
    return create_app(reporting, resources)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app, raise_app_exceptions=False) as client:
        yield client


async def refuse_credential_writes(session: AsyncSession) -> None:
    for statement in REFUSE_CREDENTIAL_WRITES:
        await session.execute(text(statement))


def assert_reported_without(sentry: CapturingTransport, *values: str) -> None:
    (event,) = sentry.events()
    error = event["exception"]["values"][-1]["value"]
    payload = sentry.payload()
    assert "credential writes are refused" in error
    assert "SQL parameters hidden" in error
    assert b"$argon2id$" not in payload
    assert [value for value in values if value.encode() in payload] == []


async def test_a_failed_password_change_sends_sentry_no_password_or_hash(
    client: AsyncClient, session: AsyncSession
) -> None:
    current = secrets.token_urlsafe(16)
    new = secrets.token_urlsafe(16)
    user = await make_user(session)
    await make_credential(session, user, password_hash=PASSWORD_HASHER.hash(current))
    headers = bearer(await make_login_session(session, user))
    await refuse_credential_writes(session)
    sentry = capture_sentry()

    response = await client.post(
        "/v1/auth/password-change",
        json={"current_password": current, "new_password": new},
        headers=headers,
    )

    assert response.status_code == 500
    assert_reported_without(sentry, current, new)


async def test_a_failed_password_reset_sends_sentry_no_password_or_hash(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    new = secrets.token_urlsafe(16)
    user = await make_user(session)
    await make_credential(session, user)
    token = await make_reset_token(session, settings.secret_key, user)
    await refuse_credential_writes(session)
    sentry = capture_sentry()

    response = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": token, "password": new}
    )

    assert response.status_code == 500
    assert_reported_without(sentry, new, token)
