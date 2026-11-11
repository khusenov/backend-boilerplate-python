import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.users.models import User
from tests.audit import audit_rows
from tests.clients import bearer, problem, reply
from tests.factories import (
    PASSWORD,
    make_code,
    make_credential,
    make_login_session,
    make_user,
    wrong_code,
)
from tests.jobs import RunJobs
from tests.outbox import Outbox, code_in

OLD_EMAIL = "ada@example.com"
NEW_EMAIL = "lovelace@example.com"


@pytest.fixture
async def user(session: AsyncSession) -> User:
    user = await make_user(session, email=OLD_EMAIL)
    await make_credential(session, user)
    return user


@pytest.fixture
async def headers(session: AsyncSession, user: User) -> dict[str, str]:
    return bearer(await make_login_session(session, user))


def change(email: str = NEW_EMAIL, password: str = PASSWORD) -> dict[str, str]:
    return {"email": email, "password": password}


def confirmation(code: str, email: str = NEW_EMAIL) -> dict[str, str]:
    return {"email": email, "code": code}


def signup(code: str) -> dict[str, str]:
    return {"email": NEW_EMAIL, "code": code, "name": "Grace", "password": PASSWORD}


async def test_a_confirmed_change_moves_the_account_and_tells_the_old_address(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    user: User,
    headers: dict[str, str],
) -> None:
    traced = {"X-Forwarded-For": "203.0.113.7"}
    requested = await client.post(
        "/v1/auth/email-change", json=change(), headers=headers
    )
    await run_jobs()
    code = code_in(outbox.sent[0])

    confirmed = await client.post(
        "/v1/auth/email-change/confirm",
        json=confirmation(code),
        headers=headers | traced,
    )
    await run_jobs()
    events = await audit_rows(session)
    old = await client.post(
        "/v1/auth/tokens", json={"email": OLD_EMAIL, "password": PASSWORD}
    )
    new = await client.post(
        "/v1/auth/tokens", json={"email": NEW_EMAIL, "password": PASSWORD}
    )

    await session.refresh(user)
    assert (requested.status_code, confirmed.status_code) == (202, 204)
    assert user.email == NEW_EMAIL
    assert [(email.to, email.subject) for email in outbox.sent] == [
        (NEW_EMAIL, "Your verification code"),
        (OLD_EMAIL, "Your email address was changed"),
    ]
    assert (problem(old), new.status_code) == ((401, "auth.invalid_credentials"), 201)
    assert events == [
        (
            user.id,
            "users.email_changed",
            user.id,
            "203.0.113.7",
            confirmed.headers["x-request-id"],
            {"email": NEW_EMAIL},
        )
    ]


async def test_changes_need_the_current_password(
    client: AsyncClient, run_jobs: RunJobs, outbox: Outbox, headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/auth/email-change",
        json=change(password="not the password"),
        headers=headers,
    )
    await run_jobs()

    assert problem(response) == (403, "auth.wrong_password")
    assert outbox.sent == []


async def test_wrong_current_passwords_spend_the_login_budget(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    wrong = [
        await client.post(
            "/v1/auth/email-change",
            json=change(f"new{n}@example.com", "not the password"),
            headers=headers,
        )
        for n in range(5)
    ]

    right = await client.post("/v1/auth/email-change", json=change(), headers=headers)

    assert [problem(guess) for guess in wrong] == [(403, "auth.wrong_password")] * 5
    assert problem(right) == (429, "auth.too_many_attempts")


async def test_a_spent_login_budget_refuses_email_changes(
    client: AsyncClient, user: User, headers: dict[str, str]
) -> None:
    wrong = {"email": user.email, "password": "not the password"}
    for _ in range(5):
        await client.post("/v1/auth/tokens", json=wrong)

    response = await client.post(
        "/v1/auth/email-change", json=change(), headers=headers
    )

    assert problem(response) == (429, "auth.too_many_attempts")


async def test_signups_and_email_changes_share_each_address_budget(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    replies: list[int] = []
    for _ in range(4):
        signup = await client.post("/v1/auth/signup", json={"email": NEW_EMAIL})
        requested = await client.post(
            "/v1/auth/email-change", json=change(), headers=headers
        )
        replies += [signup.status_code, requested.status_code]

    assert replies == [202] * 5 + [429] * 3


async def test_moves_to_a_taken_address_get_the_same_reply(
    client: AsyncClient,
    session: AsyncSession,
    run_jobs: RunJobs,
    outbox: Outbox,
    headers: dict[str, str],
) -> None:
    await make_user(session, email="taken@example.com")
    taken = await client.post(
        "/v1/auth/email-change", json=change("taken@example.com"), headers=headers
    )
    free = await client.post("/v1/auth/email-change", json=change(), headers=headers)

    await run_jobs()

    assert reply(taken) == reply(free)
    assert taken.status_code == 202
    assert [(email.to, email.subject) for email in outbox.sent] == [
        ("taken@example.com", "This email address already has an account"),
        (NEW_EMAIL, "Your verification code"),
    ]


async def test_a_signup_takes_over_an_address_an_email_change_asked_for(
    client: AsyncClient, run_jobs: RunJobs, outbox: Outbox, headers: dict[str, str]
) -> None:
    await client.post("/v1/auth/email-change", json=change(), headers=headers)
    await client.post("/v1/auth/signup", json={"email": NEW_EMAIL})
    await run_jobs()
    code = code_in(outbox.sent[-1])

    moved = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )
    signed_up = await client.post("/v1/auth/signup/confirm", json=signup(code))

    assert problem(moved) == (403, "auth.invalid_code")
    assert signed_up.status_code == 204


async def test_an_email_change_takes_over_an_address_a_signup_asked_for(
    client: AsyncClient, run_jobs: RunJobs, outbox: Outbox, headers: dict[str, str]
) -> None:
    await client.post("/v1/auth/signup", json={"email": NEW_EMAIL})
    await client.post("/v1/auth/email-change", json=change(), headers=headers)
    await run_jobs()
    code = code_in(outbox.sent[-1])

    signed_up = await client.post("/v1/auth/signup/confirm", json=signup(code))
    moved = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )

    assert problem(signed_up) == (403, "auth.invalid_code")
    assert moved.status_code == 204


async def test_codes_change_only_the_email_of_whoever_asked(
    client: AsyncClient, session: AsyncSession, settings: Settings, user: User
) -> None:
    code = await make_code(session, settings.secret_key, NEW_EMAIL, user_id=user.id)
    other = await make_user(session)
    headers = bearer(await make_login_session(session, other))

    response = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )

    assert problem(response) == (403, "auth.invalid_code")


async def test_addresses_taken_after_the_code_was_mailed_get_403(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    user: User,
    headers: dict[str, str],
) -> None:
    code = await make_code(session, settings.secret_key, NEW_EMAIL, user_id=user.id)
    await make_user(session, email=NEW_EMAIL)

    response = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )

    await session.refresh(user)
    assert problem(response) == (403, "auth.invalid_code")
    assert user.email == OLD_EMAIL


async def test_signup_codes_change_no_email(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    headers: dict[str, str],
) -> None:
    code = await make_code(session, settings.secret_key, NEW_EMAIL)

    response = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )

    assert problem(response) == (403, "auth.invalid_code")


async def test_five_wrong_guesses_shut_out_even_the_right_code(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    user: User,
    headers: dict[str, str],
) -> None:
    code = await make_code(session, settings.secret_key, NEW_EMAIL, user_id=user.id)
    guesses = [
        await client.post(
            "/v1/auth/email-change/confirm",
            json=confirmation(wrong_code(code)),
            headers=headers,
        )
        for _ in range(5)
    ]

    right = await client.post(
        "/v1/auth/email-change/confirm", json=confirmation(code), headers=headers
    )

    assert [problem(guess) for guess in guesses] == [(403, "auth.invalid_code")] * 5
    assert problem(right) == (429, "auth.too_many_attempts")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/auth/email-change", change()),
        ("/v1/auth/email-change/confirm", confirmation("123456")),
    ],
    ids=["request", "confirm"],
)
async def test_anonymous_changes_get_401(
    client: AsyncClient, path: str, body: dict[str, str]
) -> None:
    response = await client.post(path, json=body)

    assert problem(response) == (401, "unauthenticated")
