import asyncio
import secrets
import threading
from datetime import UTC, datetime

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.resources import Resources
from app.modules.auth.models import ResetToken
from app.modules.users.models import User, UserStatus
from tests.audit import audit_rows
from tests.clients import bearer, connect, problem, reply
from tests.factories import (
    PASSWORD,
    count_rows,
    make_code,
    make_credential,
    make_login_session,
    make_reset_token,
    make_user,
)
from tests.jobs import RunJobs
from tests.outbox import Outbox, SentEmail, token_in
from tests.statements import record_statements

EMAIL = "ada@example.com"
NEW_PASSWORD = "a brand new passphrase"
FORWARDED = {"X-Forwarded-For": "203.0.113.7"}


@pytest.fixture
async def user(session: AsyncSession) -> User:
    user = await make_user(session, email=EMAIL)
    await make_credential(session, user)
    return user


async def confirm(
    client: AsyncClient,
    token: str,
    password: str = NEW_PASSWORD,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "password": password},
        headers=headers,
    )


async def log_in(client: AsyncClient, password: str) -> Response:
    return await client.post(
        "/v1/auth/tokens", json={"email": EMAIL, "password": password}
    )


async def test_reset_requests_get_one_reply_for_any_address(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    await make_user(session, email=EMAIL)
    statements = await record_statements(session)
    known = await client.post("/v1/auth/password-reset", json={"email": EMAIL})
    unknown = await client.post(
        "/v1/auth/password-reset", json={"email": "nobody@example.com"}
    )
    reads = [statement for statement in statements if " FROM users" in statement]

    await run_jobs()

    minted = await session.scalars(
        select(ResetToken.email).where(ResetToken.token_hash.is_not(None))
    )
    assert reply(known) == reply(unknown)
    assert (known.status_code, known.content) == (202, b"")
    assert reads == []
    assert [(email.to, email.subject) for email in outbox.sent] == [
        (EMAIL, "Reset your password")
    ]
    assert list(minted) == [EMAIL]


async def test_the_mailed_token_sets_a_new_password(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    user: User,
) -> None:
    await client.post("/v1/auth/password-reset", json={"email": " Ada@Example.com "})
    await run_jobs()
    token = token_in(outbox.sent[0])

    confirmed = await confirm(client, token, headers=FORWARDED)
    events = await audit_rows(session)
    old = await log_in(client, PASSWORD)
    new = await log_in(client, NEW_PASSWORD)

    assert (confirmed.status_code, confirmed.content) == (204, b"")
    assert (problem(old), new.status_code) == ((401, "auth.invalid_credentials"), 201)
    assert outbox.sent == [
        SentEmail(
            to=EMAIL,
            subject="Reset your password",
            text=(
                f"To choose a new password, use this token: {token}\n\n"
                "It expires in 30 minutes. "
                "If you did not ask for it, you can ignore this email.\n"
            ),
        )
    ]
    assert events == [
        (
            user.id,
            "auth.password_reset",
            user.id,
            "203.0.113.7",
            confirmed.headers["x-request-id"],
            {},
        )
    ]


async def test_resets_give_users_without_a_password_their_first(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    await make_user(session, email=EMAIL)
    await client.post("/v1/auth/password-reset", json={"email": EMAIL})
    await run_jobs()

    confirmed = await confirm(client, token_in(outbox.sent[0]))
    login = await log_in(client, NEW_PASSWORD)

    assert (confirmed.status_code, login.status_code) == (204, 201)


async def test_a_reset_ends_every_session(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    bearers = [bearer(await make_login_session(session, user)) for _ in range(2)]
    token = await make_reset_token(session, settings.secret_key, user)

    await confirm(client, token)

    replies = [await client.get("/v1/users/me", headers=headers) for headers in bearers]
    assert [problem(response) for response in replies] == [(401, "unauthenticated")] * 2


async def test_reset_tokens_work_once(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)

    first = await confirm(client, token)
    again = await confirm(client, token, "another new passphrase")
    login = await log_in(client, NEW_PASSWORD)

    assert first.status_code == 204
    assert problem(again) == (403, "auth.invalid_token")
    assert login.status_code == 201
    assert await count_rows(session, ResetToken) == 0


async def test_only_the_newest_token_works(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    old = await make_reset_token(session, settings.secret_key, user)
    new = await make_reset_token(session, settings.secret_key, user)

    stale = await confirm(client, old)
    fresh = await confirm(client, new)

    assert problem(stale) == (403, "auth.invalid_token")
    assert fresh.status_code == 204


async def test_a_new_request_voids_the_mailed_token_at_once(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)
    await client.post("/v1/auth/password-reset", json={"email": EMAIL})

    response = await confirm(client, token)

    assert problem(response) == (403, "auth.invalid_token")


async def test_wrong_expired_and_deactivated_tokens_get_one_reply(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    expired = await make_reset_token(
        session, settings.secret_key, user, expires_at=datetime(2000, 1, 1, tzinfo=UTC)
    )
    leaver = await make_user(session)
    deactivated = await make_reset_token(session, settings.secret_key, leaver)
    leaver.status = UserStatus.DEACTIVATED
    await session.flush()
    wrong = secrets.token_urlsafe(32)

    responses = [
        await confirm(client, token) for token in (wrong, expired, deactivated)
    ]
    login = await log_in(client, PASSWORD)

    assert problem(responses[0]) == (403, "auth.invalid_token")
    assert [reply(response) for response in responses] == [reply(responses[0])] * 3
    assert login.status_code == 201


async def test_an_email_change_voids_reset_tokens(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)
    code = await make_code(
        session, settings.secret_key, "lovelace@example.com", user_id=user.id
    )
    headers = bearer(await make_login_session(session, user))
    await client.post("/v1/auth/password-reset", json={"email": "lovelace@example.com"})

    changed = await client.post(
        "/v1/auth/email-change/confirm",
        json={"email": "lovelace@example.com", "code": code},
        headers=headers,
    )
    response = await confirm(client, token)

    assert changed.status_code == 204
    assert problem(response) == (403, "auth.invalid_token")
    assert await count_rows(session, ResetToken) == 0


async def test_a_token_works_only_while_its_account_owns_its_address(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)
    user.email = "lovelace@example.com"
    await session.flush()

    moved = await confirm(client, token)
    await make_user(session, email=EMAIL)
    taken = await confirm(client, token)

    assert [problem(moved), problem(taken)] == [(403, "auth.invalid_token")] * 2


async def test_a_request_after_an_expired_one_mails_a_working_token(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    user: User,
) -> None:
    await client.post("/v1/auth/password-reset", json={"email": EMAIL})
    expired = datetime(2000, 1, 1, tzinfo=UTC)
    await session.execute(update(ResetToken).values(expires_at=expired))
    await run_jobs()
    await client.post("/v1/auth/password-reset", json={"email": EMAIL})
    await run_jobs()

    confirmations = [await confirm(client, token_in(email)) for email in outbox.sent]

    assert [response.status_code for response in confirmations] == [204]


async def test_common_passwords_get_422_and_spend_no_token(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)

    refused = await confirm(client, token, "CorrectHorseBatteryStaple")
    accepted = await confirm(client, token)

    assert problem(refused) == (422, "validation_failed")
    assert refused.json()["errors"] == [
        {"pointer": "#/password", "detail": "Value error, this password is too common"}
    ]
    assert accepted.status_code == 204


async def test_reset_requests_are_limited_per_email(client: AsyncClient) -> None:
    replies = [
        (
            await client.post("/v1/auth/password-reset", json={"email": EMAIL})
        ).status_code
        for _ in range(6)
    ]

    assert replies == [202] * 5 + [429]


async def test_reset_requests_are_limited_per_network_address(
    client: AsyncClient,
) -> None:
    replies = [
        (
            await client.post(
                "/v1/auth/password-reset",
                json={"email": f"user{n}@example.com"},
                headers=FORWARDED,
            )
        ).status_code
        for n in range(11)
    ]
    elsewhere = await client.post("/v1/auth/password-reset", json={"email": EMAIL})

    assert replies == [202] * 10 + [429]
    assert elsewhere.status_code == 202


async def test_signups_leave_the_reset_budget_alone(client: AsyncClient) -> None:
    signups = [
        (await client.post("/v1/auth/signup", json={"email": EMAIL})).status_code
        for _ in range(6)
    ]

    reset = await client.post("/v1/auth/password-reset", json={"email": EMAIL})

    assert signups == [202] * 5 + [429]
    assert reset.status_code == 202


async def test_twenty_wrong_tokens_shut_an_address_out(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    token = await make_reset_token(session, settings.secret_key, user)
    wrong = [
        await confirm(client, secrets.token_urlsafe(32), headers=FORWARDED)
        for _ in range(20)
    ]

    shut_out = await confirm(client, token, headers=FORWARDED)
    elsewhere = await confirm(client, token)

    assert [problem(response) for response in wrong] == [
        (403, "auth.invalid_token")
    ] * 20
    assert problem(shut_out) == (429, "auth.too_many_attempts")
    assert elsewhere.status_code == 204


async def test_every_read_of_a_reset_token_locks_its_row(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    user: User,
) -> None:
    statements = await record_statements(session)
    await client.post("/v1/auth/password-reset", json={"email": EMAIL})
    await run_jobs()
    await confirm(client, token_in(outbox.sent[0]))

    reads = [
        statement
        for statement in statements
        if statement.startswith("SELECT auth_reset_tokens.")
    ]
    assert {read.endswith(" FOR UPDATE") for read in reads} == {True}


@pytest.mark.committed
async def test_reset_confirmations_queued_for_hashing_hold_no_connection(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    resources: Resources,
    resume_hashing: threading.Event,
) -> None:
    async with sessionmaker.begin() as session:
        users = [await make_user(session) for _ in range(8)]
        tokens = [
            await make_reset_token(session, settings.secret_key, user) for user in users
        ]
    pool = resources.engine.pool
    assert isinstance(pool, QueuePool)
    idle = pool.checkedout()
    async with LifespanManager(app) as manager, connect(manager.app) as client:
        confirmations = asyncio.gather(*(confirm(client, token) for token in tokens))
        await asyncio.sleep(0.2)
        held = pool.checkedout() - idle
        resume_hashing.set()
        replies = await confirmations

    assert held == 4
    assert [response.status_code for response in replies] == [204] * 8


@pytest.mark.committed
async def test_a_reset_and_a_change_at_once_leave_the_winners_password(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    resume_hashing: threading.Event,
) -> None:
    async with sessionmaker.begin() as session:
        user = await make_user(session, email=EMAIL)
        await make_credential(session, user)
        headers = bearer(await make_login_session(session, user))
        token = await make_reset_token(session, settings.secret_key, user)
    change = {"current_password": PASSWORD, "new_password": "another new passphrase"}
    async with (
        LifespanManager(app) as manager,
        connect(manager.app, raise_app_exceptions=False) as client,
    ):
        replies = asyncio.gather(
            confirm(client, token),
            client.post("/v1/auth/password-change", json=change, headers=headers),
        )
        await asyncio.sleep(0.2)
        resume_hashing.set()
        reset, changed = await replies
        logins = [
            await log_in(client, password)
            for password in (NEW_PASSWORD, change["new_password"])
        ]

    statuses = [response.status_code for response in (reset, changed, *logins)]
    assert statuses in ([204, 403, 201, 401], [403, 204, 401, 201])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/auth/password-reset", {"email": "not an address"}),
        ("/v1/auth/password-reset/confirm", {"token": "a" * 42, "password": PASSWORD}),
        ("/v1/auth/password-reset/confirm", {"token": "a" * 44, "password": PASSWORD}),
        ("/v1/auth/password-reset/confirm", {"token": "+" * 43, "password": PASSWORD}),
        ("/v1/auth/password-reset/confirm", {"token": "a" * 43, "password": "x" * 14}),
    ],
    ids=["address", "short token", "long token", "token alphabet", "short password"],
)
async def test_malformed_reset_bodies_get_422(
    client: AsyncClient, path: str, body: dict[str, str]
) -> None:
    response = await client.post(path, json=body)

    assert problem(response) == (422, "validation_failed")
