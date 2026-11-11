import asyncio
import threading

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.http.deps import SESSION_COOKIE
from app.modules.users.models import User
from tests.audit import audit_rows
from tests.clients import bearer, connect, problem
from tests.factories import (
    PASSWORD,
    make_credential,
    make_login_session,
    make_reset_token,
    make_user,
)
from tests.jobs import RunJobs
from tests.outbox import Outbox, SentEmail

EMAIL = "ada@example.com"
NEW_PASSWORD = "a brand new passphrase"
TRUSTED = {"Origin": "https://app.example.com"}


@pytest.fixture
async def user(session: AsyncSession) -> User:
    user = await make_user(session, email=EMAIL)
    await make_credential(session, user)
    return user


@pytest.fixture
async def headers(session: AsyncSession, user: User) -> dict[str, str]:
    return bearer(await make_login_session(session, user))


def change(current: str = PASSWORD, new: str = NEW_PASSWORD) -> dict[str, str]:
    return {"current_password": current, "new_password": new}


async def log_in(client: AsyncClient, password: str) -> Response:
    return await client.post(
        "/v1/auth/tokens", json={"email": EMAIL, "password": password}
    )


async def test_a_change_ends_every_session_and_tells_the_address(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    user: User,
    headers: dict[str, str],
) -> None:
    cookie = await make_login_session(session, user)
    client.cookies.set(SESSION_COOKIE, cookie)
    traced = TRUSTED | {"X-Forwarded-For": "203.0.113.7"}

    changed = await client.post(
        "/v1/auth/password-change", json=change(), headers=traced
    )
    events = await audit_rows(session)
    await run_jobs()
    replays = [
        await client.get("/v1/users/me"),
        await client.get("/v1/users/me", headers=headers),
    ]
    old = await log_in(client, PASSWORD)
    new = await log_in(client, NEW_PASSWORD)

    cleared = changed.headers["set-cookie"]
    assert (changed.status_code, changed.content) == (204, b"")
    assert cleared.startswith(f'{SESSION_COOKIE}=""; expires=')
    assert cleared.endswith("; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure")
    assert [problem(replay) for replay in replays] == [(401, "unauthenticated")] * 2
    assert (problem(old), new.status_code) == ((401, "auth.invalid_credentials"), 201)
    assert outbox.sent == [
        SentEmail(
            to=EMAIL,
            subject="Your password was changed",
            text=(
                "The password of your account was changed. "
                "If you did not change it, reset it at once.\n"
            ),
        )
    ]
    assert events == [
        (
            user.id,
            "auth.password_changed",
            user.id,
            "203.0.113.7",
            changed.headers["x-request-id"],
            {},
        )
    ]


async def test_changes_need_the_current_password(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/auth/password-change",
        json=change(current="not the password"),
        headers=headers,
    )
    events = await audit_rows(session)
    await run_jobs()
    me = await client.get("/v1/users/me", headers=headers)
    login = await log_in(client, PASSWORD)

    assert problem(response) == (403, "auth.wrong_password")
    assert (me.status_code, login.status_code) == (200, 201)
    assert (events, outbox.sent) == ([], [])


async def test_wrong_current_passwords_spend_the_login_budget(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    wrong = [
        await client.post(
            "/v1/auth/password-change",
            json=change(current="not the password"),
            headers=headers,
        )
        for _ in range(5)
    ]

    right = await client.post(
        "/v1/auth/password-change", json=change(), headers=headers
    )

    assert [problem(guess) for guess in wrong] == [(403, "auth.wrong_password")] * 5
    assert problem(right) == (429, "auth.too_many_attempts")


async def test_a_spent_login_budget_refuses_password_changes(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    for _ in range(5):
        await log_in(client, "not the password")

    response = await client.post(
        "/v1/auth/password-change", json=change(), headers=headers
    )

    assert problem(response) == (429, "auth.too_many_attempts")


async def test_a_password_change_voids_reset_tokens(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    user: User,
    headers: dict[str, str],
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)

    changed = await client.post(
        "/v1/auth/password-change", json=change(), headers=headers
    )
    reset = await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "password": "another new passphrase"},
    )

    assert changed.status_code == 204
    assert problem(reset) == (403, "auth.invalid_token")


async def test_a_password_change_voids_reset_requests_not_yet_mailed(
    client: AsyncClient, run_jobs: RunJobs, outbox: Outbox, headers: dict[str, str]
) -> None:
    requested = await client.post("/v1/auth/password-reset", json={"email": EMAIL})

    changed = await client.post(
        "/v1/auth/password-change", json=change(), headers=headers
    )
    await run_jobs()

    assert (requested.status_code, changed.status_code) == (202, 204)
    assert [email.subject for email in outbox.sent] == ["Your password was changed"]


async def test_common_new_passwords_get_422(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/auth/password-change",
        json=change(new="CorrectHorseBatteryStaple"),
        headers=headers,
    )
    login = await log_in(client, PASSWORD)

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [
        {
            "pointer": "#/new_password",
            "detail": "Value error, this password is too common",
        }
    ]
    assert login.status_code == 201


@pytest.mark.committed
async def test_two_changes_at_once_leave_the_winners_password(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    resume_hashing: threading.Event,
) -> None:
    async with sessionmaker.begin() as session:
        user = await make_user(session, email=EMAIL)
        await make_credential(session, user)
        bearers = [bearer(await make_login_session(session, user)) for _ in range(2)]
    passwords = [NEW_PASSWORD, "another new passphrase"]
    async with (
        LifespanManager(app) as manager,
        connect(manager.app, raise_app_exceptions=False) as client,
    ):
        replies = asyncio.gather(
            *(
                client.post(
                    "/v1/auth/password-change", json=change(new=new), headers=headers
                )
                for new, headers in zip(passwords, bearers, strict=True)
            )
        )
        await asyncio.sleep(0.2)
        resume_hashing.set()
        changes = await replies
        logins = [await log_in(client, password) for password in passwords]

    statuses = [response.status_code for response in (*changes, *logins)]
    assert statuses in ([204, 403, 201, 401], [403, 204, 401, 201])


@pytest.mark.parametrize(
    "body",
    [
        change(new="x" * 14),
        {"new_password": NEW_PASSWORD},
        change() | {"password": PASSWORD},
    ],
    ids=["short password", "no current password", "unknown field"],
)
async def test_malformed_changes_get_422(
    client: AsyncClient, headers: dict[str, str], body: dict[str, str]
) -> None:
    response = await client.post("/v1/auth/password-change", json=body, headers=headers)

    assert problem(response) == (422, "validation_failed")


async def test_anonymous_changes_get_401(client: AsyncClient) -> None:
    response = await client.post("/v1/auth/password-change", json=change())

    assert problem(response) == (401, "unauthenticated")
