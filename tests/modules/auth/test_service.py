import time

import pytest
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.errors import TooManyAttemptsError
from app.modules.auth.models import Credential
from app.modules.auth.service import (
    LOGINS_PER_ADDRESS,
    LOGINS_PER_EMAIL,
    PASSWORD_HASHER,
    count_login_attempt,
    dummy_hash,
    find_credential,
    network_key,
    password_matches,
)
from tests.factories import PASSWORD, make_credential, make_user

EMAIL = "ada@example.com"


@pytest.fixture
def limiter() -> MovingWindowRateLimiter:
    return MovingWindowRateLimiter(MemoryStorage())


async def attempt(
    limiter: MovingWindowRateLimiter, address: str, email: str = EMAIL
) -> bool:
    try:
        await count_login_attempt(limiter, address, email)
    except TooManyAttemptsError:
        return False
    return True


@pytest.mark.parametrize(
    ("client_ip", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::"),
        ("fe80::1%eth0", "fe80::"),
        ("testclient", "unknown"),
        (None, "unknown"),
    ],
)
def test_clients_are_keyed_by_address_and_ipv6_by_64(
    client_ip: str | None, key: str
) -> None:
    assert network_key(client_ip) == key


async def test_an_address_gets_five_attempts_per_email(
    limiter: MovingWindowRateLimiter,
) -> None:
    replies = [await attempt(limiter, "203.0.113.7") for _ in range(6)]
    other_email = await attempt(limiter, "203.0.113.7", "bob@example.com")

    assert replies == [True] * 5 + [False]
    assert other_email


async def test_an_ipv6_network_shares_one_budget(
    limiter: MovingWindowRateLimiter,
) -> None:
    replies = [await attempt(limiter, f"2001:db8:1:2::{n + 1:x}") for n in range(6)]
    other_network = await attempt(limiter, "2001:db8:1:3::1")

    assert replies == [True] * 5 + [False]
    assert other_network


async def test_an_email_gets_100_attempts_an_hour(
    limiter: MovingWindowRateLimiter,
) -> None:
    replies = [
        await attempt(limiter, f"198.51.100.{n}") for n in range(20) for _ in range(5)
    ]
    fresh_address = await attempt(limiter, "192.0.2.1")

    assert all(replies)
    assert not fresh_address


async def test_refused_attempts_leave_the_email_budget_alone(
    limiter: MovingWindowRateLimiter,
) -> None:
    flood = [await attempt(limiter, "203.0.113.7") for _ in range(50)]
    others = [
        await attempt(limiter, f"198.51.100.{n}") for n in range(19) for _ in range(5)
    ]
    one_more = await attempt(limiter, "192.0.2.1")

    assert flood.count(True) == 5
    assert all(others)
    assert not one_more


async def test_login_budgets_refill_an_hour_after_each_attempt(
    limiter: MovingWindowRateLimiter,
) -> None:
    started = time.time()
    await attempt(limiter, "203.0.113.7")

    per_address = await limiter.get_window_stats(
        LOGINS_PER_ADDRESS, "login", "203.0.113.7", EMAIL
    )
    per_email = await limiter.get_window_stats(LOGINS_PER_EMAIL, "login", EMAIL)

    assert (per_address.remaining, per_email.remaining) == (4, 99)
    assert per_address.reset_time - started == pytest.approx(3600, abs=1)
    assert per_email.reset_time - started == pytest.approx(3600, abs=1)


def test_only_the_right_password_matches() -> None:
    password_hash = PASSWORD_HASHER.hash(PASSWORD)

    assert password_matches(password_hash, PASSWORD)
    assert not password_matches(password_hash, "not the password")
    assert not password_matches("not-a-hash", PASSWORD)
    assert not password_matches(None, PASSWORD)


def test_the_dummy_hash_costs_what_a_real_one_does() -> None:
    assert not PASSWORD_HASHER.check_needs_rehash(dummy_hash())


async def test_credential_lookups_refresh_the_row(session: AsyncSession) -> None:
    user = await make_user(session)
    credential = await make_credential(session, user)
    await session.execute(
        update(Credential)
        .where(Credential.id == credential.id)
        .values(password_hash="changed"),
        execution_options={"synchronize_session": False},
    )

    await find_credential(session, user.email)

    assert credential.password_hash == "changed"
