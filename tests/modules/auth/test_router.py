import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from argon2 import PasswordHasher
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.http.deps import SESSION_COOKIE
from app.core.resources import Resources
from app.modules.auth.models import Credential, LoginSession
from app.modules.auth.service import PASSWORD_HASHER, dummy_hash
from app.modules.users import service as users
from app.modules.users.models import User, UserStatus
from tests.clients import bearer, connect, problem
from tests.factories import PASSWORD, make_credential, make_login_session, make_user

TRUSTED = {"Origin": "https://app.example.com"}
WRONG_PASSWORD = "not the password"


class RecordingExecutor(ThreadPoolExecutor):
    def __init__(self) -> None:
        super().__init__(max_workers=4)
        self.calls: list[str] = []

    @override
    def submit[**P, T](
        self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> Future[T]:
        self.calls.append(fn.__name__)
        return super().submit(fn, *args, **kwargs)


@pytest.fixture
def hashing() -> Iterator[RecordingExecutor]:
    with RecordingExecutor() as executor:
        yield executor


@pytest.fixture
def resources(resources: Resources, hashing: RecordingExecutor) -> Resources:
    return replace(resources, hashing=hashing)


@pytest.fixture
async def resume_hashing(hashing: RecordingExecutor) -> AsyncIterator[threading.Event]:
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    busy = [loop.run_in_executor(hashing, resume.wait, 5) for _ in range(4)]
    yield resume
    resume.set()
    await asyncio.gather(*busy)


@pytest.fixture
async def user(session: AsyncSession) -> User:
    user = await make_user(session)
    await make_credential(session, user)
    return user


def login(user: User, password: str = PASSWORD) -> dict[str, str]:
    return {"email": user.email, "password": password}


async def test_cookie_logins_carry_no_token(client: AsyncClient, user: User) -> None:
    response = await client.post("/v1/auth/session", json=login(user), headers=TRUSTED)

    token = response.cookies[SESSION_COOKIE]
    assert (response.status_code, response.content) == (204, b"")
    assert "content-type" not in response.headers
    assert response.headers["set-cookie"] == (
        f"__Host-session={token}; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax; "
        "Secure"
    )
    assert [name for name, value in response.headers.items() if token in value] == [
        "set-cookie"
    ]


async def test_token_logins_return_a_bearer_token(
    client: AsyncClient, user: User
) -> None:
    response = await client.post("/v1/auth/tokens", json=login(user))

    body = response.json()
    expires_in = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
    assert response.status_code == 201
    assert sorted(body) == ["expires_at", "token"]
    assert body["expires_at"].endswith("Z")
    assert timedelta(days=30, minutes=-1) < expires_in <= timedelta(days=30)
    assert "set-cookie" not in response.headers
    logout = await client.delete("/v1/auth/session", headers=bearer(body["token"]))
    assert logout.status_code == 204


async def test_sessions_store_only_the_sha256_of_the_token(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    response = await client.post("/v1/auth/tokens", json=login(user))

    token = response.json()["token"]
    stored = await session.scalar(select(LoginSession.token_hash))
    assert len(token) == 43
    assert stored == hashlib.sha256(token.encode()).digest()


async def test_wrong_passwords_get_401(client: AsyncClient, user: User) -> None:
    response = await client.post("/v1/auth/tokens", json=login(user, WRONG_PASSWORD))

    assert problem(response) == (401, "auth.invalid_credentials")
    assert response.headers["www-authenticate"] == "Bearer"


async def test_unknown_emails_get_the_same_reply_after_a_dummy_hash(
    client: AsyncClient, user: User
) -> None:
    calls = dummy_hash.cache_info()
    unknown = await client.post(
        "/v1/auth/tokens", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = await client.post("/v1/auth/tokens", json=login(user, WRONG_PASSWORD))

    after = dummy_hash.cache_info()
    assert (after.hits + after.misses) - (calls.hits + calls.misses) == 1
    assert unknown.json() | {"request_id": ""} == wrong.json() | {"request_id": ""}
    assert unknown.status_code == wrong.status_code == 401


async def test_logins_verify_on_the_hashing_threads(
    client: AsyncClient, hashing: RecordingExecutor
) -> None:
    unknown = {"email": "nobody@example.com", "password": PASSWORD}
    await client.post("/v1/auth/tokens", json=unknown)

    assert hashing.calls == ["password_matches"]


async def test_logins_normalize_the_email(client: AsyncClient, user: User) -> None:
    typed = f" {user.email.upper()} "
    response = await client.post("/v1/auth/tokens", json=login(user) | {"email": typed})

    assert response.status_code == 201


async def test_users_without_a_password_get_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    user = await make_user(session)
    response = await client.post("/v1/auth/tokens", json=login(user))

    assert problem(response) == (401, "auth.invalid_credentials")


async def test_deactivated_users_cannot_log_in(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    user.status = UserStatus.DEACTIVATED
    await session.flush()
    response = await client.post("/v1/auth/tokens", json=login(user))

    assert problem(response) == (401, "auth.invalid_credentials")


async def test_logins_rehash_outdated_password_hashes(
    client: AsyncClient, session: AsyncSession, hashing: RecordingExecutor
) -> None:
    user = await make_user(session)
    outdated = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    credential = await make_credential(session, user, password_hash=outdated)
    response = await client.post("/v1/auth/tokens", json=login(user))

    await session.refresh(credential)
    assert response.status_code == 201
    assert credential.password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert PASSWORD_HASHER.verify(credential.password_hash, PASSWORD)
    assert hashing.calls == ["password_matches", "hash"]


async def test_logins_keep_current_password_hashes(
    client: AsyncClient, session: AsyncSession
) -> None:
    user = await make_user(session)
    credential = await make_credential(session, user)
    current = credential.password_hash
    response = await client.post("/v1/auth/tokens", json=login(user))

    await session.refresh(credential)
    assert response.status_code == 201
    assert credential.password_hash == current


async def test_revoked_cookie_sessions_get_401(client: AsyncClient, user: User) -> None:
    await client.post("/v1/auth/session", json=login(user), headers=TRUSTED)
    token = client.cookies[SESSION_COOKIE]
    logout = await client.delete("/v1/auth/session", headers=TRUSTED)
    stored = SESSION_COOKIE in client.cookies
    client.cookies.set(SESSION_COOKIE, token)
    replay = await client.delete("/v1/auth/session", headers=TRUSTED)

    cleared = logout.headers["set-cookie"]
    assert logout.status_code == 204
    assert "content-type" not in logout.headers
    assert cleared.startswith(f'{SESSION_COOKIE}=""; expires=')
    assert cleared.endswith("; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure")
    assert not stored
    assert problem(replay) == (401, "unauthenticated")


async def test_revoked_bearer_tokens_get_401(client: AsyncClient, user: User) -> None:
    token = (await client.post("/v1/auth/tokens", json=login(user))).json()["token"]
    logout = await client.delete("/v1/auth/session", headers=bearer(token))
    replay = await client.delete("/v1/auth/session", headers=bearer(token))

    assert logout.status_code == 204
    assert problem(replay) == (401, "unauthenticated")


async def test_logouts_end_only_the_current_session(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    ended = await make_login_session(session, user)
    kept = await make_login_session(session, user)
    await client.delete("/v1/auth/session", headers=bearer(ended))

    response = await client.delete("/v1/auth/session", headers=bearer(kept))
    assert response.status_code == 204


async def test_expired_sessions_get_401(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    expired = datetime(2000, 1, 1, tzinfo=UTC)
    token = await make_login_session(session, user, expires_at=expired)
    response = await client.delete("/v1/auth/session", headers=bearer(token))

    assert problem(response) == (401, "unauthenticated")


async def test_deactivated_users_sessions_get_401(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    token = await make_login_session(session, user)
    await users.deactivate_user(session, user.id)
    response = await client.delete("/v1/auth/session", headers=bearer(token))

    assert problem(response) == (401, "unauthenticated")


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.example.com"}, {}], ids=["foreign", "none"]
)
async def test_cookie_logins_from_untrusted_origins_get_403(
    client: AsyncClient, user: User, headers: dict[str, str]
) -> None:
    response = await client.post("/v1/auth/session", json=login(user), headers=headers)

    assert problem(response) == (403, "untrusted_origin")
    assert "set-cookie" not in response.headers


async def test_foreign_origin_cookie_logouts_get_403(
    client: AsyncClient, session: AsyncSession, user: User
) -> None:
    token = await make_login_session(session, user)
    client.cookies.set(SESSION_COOKIE, token)
    foreign = await client.delete(
        "/v1/auth/session", headers={"Origin": "https://evil.example.com"}
    )
    trusted = await client.delete("/v1/auth/session", headers=TRUSTED)

    assert problem(foreign) == (403, "untrusted_origin")
    assert trusted.status_code == 204


async def test_untrusted_origins_spend_no_login_attempts(
    client: AsyncClient, user: User
) -> None:
    foreign = {"Origin": "https://evil.example.com"}
    for _ in range(5):
        await client.post("/v1/auth/session", json=login(user), headers=foreign)
    response = await client.post("/v1/auth/session", json=login(user), headers=TRUSTED)

    assert response.status_code == 204


async def test_both_logins_share_one_budget(client: AsyncClient, user: User) -> None:
    for _ in range(5):
        await client.post("/v1/auth/tokens", json=login(user, WRONG_PASSWORD))
    response = await client.post("/v1/auth/session", json=login(user), headers=TRUSTED)

    assert problem(response) == (429, "auth.too_many_attempts")


async def test_attempts_on_unknown_emails_count_too(client: AsyncClient) -> None:
    unknown = {"email": "nobody@example.com", "password": PASSWORD}
    replies = [
        (await client.post("/v1/auth/tokens", json=unknown)).status_code
        for _ in range(6)
    ]

    assert replies == [401, 401, 401, 401, 401, 429]


@pytest.mark.committed
async def test_five_of_ten_concurrent_bad_logins_get_429_while_another_ip_logs_in(
    app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker.begin() as session:
        user = await make_user(session)
        await make_credential(session, user)
    async with (
        LifespanManager(app) as manager,
        connect(manager.app, address="203.0.113.7") as attacker,
        connect(manager.app, address="2001:db8::7") as neighbour,
    ):
        attempts = [
            attacker.post("/v1/auth/tokens", json=login(user, WRONG_PASSWORD))
            for _ in range(10)
        ]
        refused = await asyncio.gather(*attempts)
        accepted = await neighbour.post("/v1/auth/tokens", json=login(user))

    assert sorted(problem(response) for response in refused) == [
        *[(401, "auth.invalid_credentials")] * 5,
        *[(429, "auth.too_many_attempts")] * 5,
    ]
    assert accepted.status_code == 201


@pytest.mark.committed
async def test_logins_queued_for_hashing_hold_no_connection(
    app: FastAPI, resources: Resources, resume_hashing: threading.Event
) -> None:
    pool = resources.engine.pool
    assert isinstance(pool, QueuePool)
    idle = pool.checkedout()
    async with LifespanManager(app) as manager, connect(manager.app) as client:
        logins = asyncio.gather(
            *(
                client.post(
                    "/v1/auth/tokens",
                    json={"email": f"nobody{n}@example.com", "password": PASSWORD},
                )
                for n in range(8)
            )
        )
        await asyncio.sleep(0.2)
        held = pool.checkedout() - idle
        resume_hashing.set()
        await logins

    assert held == 4


@pytest.mark.committed
async def test_password_changes_during_a_login_survive_its_rehash(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    resume_hashing: threading.Event,
) -> None:
    outdated = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    changed = PASSWORD_HASHER.hash("a new password")
    async with sessionmaker.begin() as session:
        user = await make_user(session)
        await make_credential(session, user, password_hash=outdated)

    async def change_password() -> None:
        async with sessionmaker.begin() as session:
            await session.execute(
                update(Credential)
                .where(Credential.user_id == user.id)
                .values(password_hash=changed)
            )

    async with (
        LifespanManager(app) as manager,
        connect(manager.app) as client,
        asyncio.TaskGroup() as group,
    ):
        logging_in = group.create_task(client.post("/v1/auth/tokens", json=login(user)))
        await asyncio.sleep(0.2)
        group.create_task(change_password())
        await asyncio.sleep(0.2)
        resume_hashing.set()

    async with sessionmaker() as session:
        stored = await session.scalar(
            select(Credential.password_hash).where(Credential.user_id == user.id)
        )
    assert logging_in.result().status_code == 201
    assert stored == changed
