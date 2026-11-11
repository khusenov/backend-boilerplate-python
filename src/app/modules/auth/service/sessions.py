import asyncio
import secrets
import uuid
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta

from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import flush
from app.core.security import Principal
from app.modules.auth.errors import InvalidCredentialsError
from app.modules.auth.models import Credential, LoginSession
from app.modules.auth.schemas import Login
from app.modules.auth.service.credentials import (
    PASSWORD_HASHER,
    hash_password,
    hash_token,
    verify_password,
)
from app.modules.auth.service.limits import count_login_attempt
from app.modules.users import public as users

SESSION_LIFETIME = timedelta(days=30)


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
    audit.record(
        session,
        "auth.logged_in",
        actor_id=credential.user_id,
        target_id=credential.user_id,
        client_ip=client_ip,
        changes={},
    )
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
    return Principal(user_id=user.id, session_id=login.id, role=user.role)


async def log_out(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(delete(LoginSession).where(LoginSession.id == session_id))
