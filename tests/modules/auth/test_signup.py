import asyncio
import threading
from datetime import UTC, datetime

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.resources import Resources
from app.modules.auth.models import Credential, VerificationCode
from app.modules.auth.service.limits import GUESSES_PER_EMAIL
from app.modules.users.models import User
from tests.clients import connect, problem, reply
from tests.factories import PASSWORD, count_rows, make_code, make_user, wrong_code
from tests.jobs import RunJobs
from tests.outbox import Outbox, SentEmail, code_in
from tests.statements import record_statements

EMAIL = "ada@example.com"


def confirmation(
    code: str, email: str = EMAIL, password: str = PASSWORD
) -> dict[str, str]:
    return {"email": email, "code": code, "name": "Ada Lovelace", "password": password}


async def test_taken_addresses_get_the_same_signup_reply(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    await make_user(session, email="taken@example.com")
    statements = await record_statements(session)
    taken = await client.post("/v1/auth/signup", json={"email": "taken@example.com"})
    new = await client.post("/v1/auth/signup", json={"email": "new@example.com"})
    reads = [statement for statement in statements if " FROM users" in statement]

    await run_jobs()

    minted = await session.scalars(
        select(VerificationCode.email).where(VerificationCode.code_mac.is_not(None))
    )
    assert reply(taken) == reply(new)
    assert (new.status_code, new.content) == (202, b"")
    assert reads == []
    assert [(email.to, email.subject) for email in outbox.sent] == [
        ("taken@example.com", "This email address already has an account"),
        ("new@example.com", "Your verification code"),
    ]
    assert outbox.sent[0].text == (
        "Someone asked to use this email address, which already belongs "
        "to an account. If it was you, sign in with it.\n"
    )
    assert list(minted) == ["new@example.com"]


async def test_the_mailed_code_and_a_password_create_the_account(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    await client.post("/v1/auth/signup", json={"email": " Ada@Example.com "})
    await run_jobs()
    code = code_in(outbox.sent[0])

    confirmed = await client.post("/v1/auth/signup/confirm", json=confirmation(code))
    login = await client.post(
        "/v1/auth/tokens", json={"email": EMAIL, "password": PASSWORD}
    )
    await run_jobs()

    user = await session.scalar(select(User).where(User.email == EMAIL))
    assert (confirmed.status_code, confirmed.content) == (204, b"")
    assert login.status_code == 201
    assert user is not None
    assert user.name == "Ada Lovelace"
    assert outbox.sent == [
        SentEmail(
            to=EMAIL,
            subject="Your verification code",
            text=(
                f"Your verification code is {code}. It expires in 15 minutes.\n\n"
                "If you did not ask for it, you can ignore this email.\n"
            ),
        ),
        SentEmail(
            to=EMAIL,
            subject="Welcome",
            text="Hi Ada Lovelace,\n\nYour account is ready.\n",
        ),
    ]


async def test_common_passwords_get_422(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    resources: Resources,
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)
    common = confirmation(code, password="CorrectHorseBatteryStaple")

    refused = await client.post("/v1/auth/signup/confirm", json=common)
    users = await count_rows(session, User)
    guesses = await resources.limiter.get_window_stats(
        GUESSES_PER_EMAIL, "guess", EMAIL
    )
    accepted = await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    assert problem(refused) == (422, "validation_failed")
    assert refused.json()["errors"] == [
        {"pointer": "#/password", "detail": "Value error, this password is too common"}
    ]
    assert (users, guesses.remaining) == (0, 5)
    assert accepted.status_code == 204


async def test_expired_codes_get_403(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    code = await make_code(
        session, settings.secret_key, EMAIL, expires_at=datetime(2000, 1, 1, tzinfo=UTC)
    )

    response = await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    assert problem(response) == (403, "auth.invalid_code")
    assert await count_rows(session, User) == 0


async def test_five_wrong_guesses_shut_out_even_the_right_code(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)
    other = await make_code(session, settings.secret_key, "bob@example.com")
    guesses = [
        await client.post(
            "/v1/auth/signup/confirm", json=confirmation(wrong_code(code))
        )
        for _ in range(5)
    ]

    right = await client.post("/v1/auth/signup/confirm", json=confirmation(code))
    elsewhere = await client.post(
        "/v1/auth/signup/confirm", json=confirmation(other, "bob@example.com")
    )

    assert [problem(guess) for guess in guesses] == [(403, "auth.invalid_code")] * 5
    assert problem(right) == (429, "auth.too_many_attempts")
    assert elsewhere.status_code == 204
    assert await count_rows(session, VerificationCode) == 1


async def test_codes_work_once(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)

    first = await client.post("/v1/auth/signup/confirm", json=confirmation(code))
    again = await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    assert first.status_code == 204
    assert problem(again) == (403, "auth.invalid_code")
    assert await count_rows(session, VerificationCode) == 0


async def test_addresses_taken_after_the_code_was_mailed_get_403(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)
    await make_user(session, email=EMAIL)

    response = await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    assert problem(response) == (403, "auth.invalid_code")
    assert await count_rows(session, Credential) == 0


async def test_only_the_newest_code_works(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    old = await make_code(session, settings.secret_key, EMAIL)
    new = old
    while new == old:
        new = await make_code(session, settings.secret_key, EMAIL)

    stale = await client.post("/v1/auth/signup/confirm", json=confirmation(old))
    fresh = await client.post("/v1/auth/signup/confirm", json=confirmation(new))

    assert problem(stale) == (403, "auth.invalid_code")
    assert fresh.status_code == 204


async def test_a_new_request_voids_the_mailed_code_at_once(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)
    await client.post("/v1/auth/signup", json={"email": EMAIL})

    response = await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    assert problem(response) == (403, "auth.invalid_code")


async def test_wrong_codes_and_addresses_without_a_code_get_the_same_reply(
    client: AsyncClient, session: AsyncSession, settings: Settings, run_jobs: RunJobs
) -> None:
    code = await make_code(session, settings.secret_key, EMAIL)
    await make_user(session, email="taken@example.com")
    await client.post("/v1/auth/signup", json={"email": "taken@example.com"})
    await run_jobs()
    guess = wrong_code(code)

    responses = [
        await client.post("/v1/auth/signup/confirm", json=confirmation(guess, email))
        for email in [EMAIL, "taken@example.com", "nobody@example.com"]
    ]

    assert problem(responses[0]) == (403, "auth.invalid_code")
    assert [reply(response) for response in responses] == [reply(responses[0])] * 3


async def test_email_change_codes_create_no_account(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    user = await make_user(session)
    code = await make_code(
        session, settings.secret_key, "new@example.com", user_id=user.id
    )

    response = await client.post(
        "/v1/auth/signup/confirm", json=confirmation(code, "new@example.com")
    )

    assert problem(response) == (403, "auth.invalid_code")


async def test_every_read_of_a_code_locks_its_row(
    client: AsyncClient, session: AsyncSession, run_jobs: RunJobs, outbox: Outbox
) -> None:
    statements = await record_statements(session)
    await client.post("/v1/auth/signup", json={"email": EMAIL})
    await run_jobs()
    code = code_in(outbox.sent[0])
    await client.post("/v1/auth/signup/confirm", json=confirmation(code))

    reads = [
        statement
        for statement in statements
        if statement.startswith("SELECT auth_verification_codes.")
    ]
    assert [read.endswith(" FOR UPDATE") for read in reads] == [True, True]


@pytest.mark.committed
async def test_confirmations_queued_for_hashing_hold_no_connection(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    resources: Resources,
    resume_hashing: threading.Event,
) -> None:
    emails = [f"signup{n}@example.com" for n in range(8)]
    async with sessionmaker.begin() as session:
        codes = [
            await make_code(session, settings.secret_key, email) for email in emails
        ]
    pool = resources.engine.pool
    assert isinstance(pool, QueuePool)
    idle = pool.checkedout()
    async with LifespanManager(app) as manager, connect(manager.app) as client:
        confirmations = asyncio.gather(
            *(
                client.post("/v1/auth/signup/confirm", json=confirmation(code, email))
                for code, email in zip(codes, emails, strict=True)
            )
        )
        await asyncio.sleep(0.2)
        held = pool.checkedout() - idle
        resume_hashing.set()
        replies = await confirmations

    assert held == 4
    assert [response.status_code for response in replies] == [204] * 8


async def test_signups_are_limited_per_email(client: AsyncClient) -> None:
    replies = [
        (await client.post("/v1/auth/signup", json={"email": EMAIL})).status_code
        for _ in range(6)
    ]

    assert replies == [202, 202, 202, 202, 202, 429]


@pytest.mark.parametrize(
    "body",
    [
        confirmation("12345"),
        confirmation("1234567"),
        confirmation("12345a"),
        confirmation("123456") | {"name": " "},
        confirmation("123456", password="x" * 14),
    ],
    ids=["short code", "long code", "letters", "blank name", "short password"],
)
async def test_malformed_confirmations_get_422(
    client: AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post("/v1/auth/signup/confirm", json=body)

    assert problem(response) == (422, "validation_failed")
