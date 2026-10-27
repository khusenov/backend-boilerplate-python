import asyncio
import hashlib
import ipaddress
import secrets
import uuid
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from limits import parse
from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import flush
from app.core.security import Principal
from app.modules.auth.errors import InvalidCredentialsError, TooManyAttemptsError
from app.modules.auth.models import Credential, LoginSession
from app.modules.auth.schemas import Login
from app.modules.users import public as users

SESSION_LIFETIME = timedelta(days=30)
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
LOGINS_PER_ADDRESS = parse("5/hour")
LOGINS_PER_EMAIL = parse("100/hour")


def network_key(client_ip: str | None) -> str:
    try:
        address = ipaddress.ip_address(client_ip or "")
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return str(ipaddress.IPv6Network((address, 64), strict=False).network_address)


@cache
def dummy_hash() -> str:
    return PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def password_matches(password_hash: str | None, password: str) -> bool:
    try:
        PASSWORD_HASHER.verify(password_hash or dummy_hash(), password)
    except VerificationError, InvalidHashError:
        return False
    return password_hash is not None


async def verify_password(
    hashing: Executor, password_hash: str | None, password: str
) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        hashing, password_matches, password_hash, password
    )


async def hash_password(hashing: Executor, password: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(hashing, PASSWORD_HASHER.hash, password)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def count_login_attempt(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    if not await limiter.hit(LOGINS_PER_ADDRESS, "login", network, email):
        raise TooManyAttemptsError
    if not await limiter.hit(LOGINS_PER_EMAIL, "login", email):
        raise TooManyAttemptsError


async def find_credential(session: AsyncSession, email: str) -> Credential | None:
    user = await users.find_user_by_email(session, email)
    if user is None or not user.active:
        return None
    statement = (
        select(Credential)
        .where(Credential.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def start_session(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[str, LoginSession]:
    token = secrets.token_urlsafe(32)
    login = LoginSession(
        user_id=user_id,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + SESSION_LIFETIME,
    )
    session.add(login)
    await flush(session, {})
    return token, login


async def log_in(
    session: AsyncSession,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    hashing_slots: asyncio.Semaphore,
    client_ip: str | None,
    data: Login,
) -> tuple[str, LoginSession]:
    await count_login_attempt(limiter, client_ip, data.email)
    async with hashing_slots:
        credential = await find_credential(session, data.email)
        password_hash = None if credential is None else credential.password_hash
        matches = await verify_password(hashing, password_hash, data.password)
        if credential is None or not matches:
            raise InvalidCredentialsError
        if PASSWORD_HASHER.check_needs_rehash(credential.password_hash):
            credential.password_hash = await hash_password(hashing, data.password)
    return await start_session(session, credential.user_id)


async def authenticate(session: AsyncSession, token: str) -> Principal | None:
    login = await session.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == hash_token(token),
            LoginSession.expires_at > datetime.now(UTC),
        )
    )
    if login is None:
        return None
    user = await users.get_user(session, login.user_id)
    if not user.active:
        return None
    return Principal(user_id=user.id, session_id=login.id)


async def log_out(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(delete(LoginSession).where(LoginSession.id == session_id))
