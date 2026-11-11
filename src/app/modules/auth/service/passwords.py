import asyncio
import base64
import hmac
import secrets
import uuid
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta

from limits.aio.strategies import MovingWindowRateLimiter
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import flush
from app.core.jobs.queue import JobQueue
from app.core.security import Principal
from app.modules.auth.errors import InvalidTokenError
from app.modules.auth.models import ResetToken
from app.modules.auth.schemas import (
    PasswordChange,
    PasswordReset,
    PasswordResetConfirm,
    PasswordSet,
    SendPasswordChangedNotice,
    SendResetToken,
)
from app.modules.auth.service.credentials import (
    check_password,
    hash_token,
    set_password,
    void_reset_tokens,
)
from app.modules.auth.service.limits import (
    count_login_attempt,
    count_reset_confirmation,
    count_reset_request,
)
from app.modules.users import public as users

RESET_LIFETIME = timedelta(minutes=30)


def derive_token(secret_key: SecretStr, seed: bytes) -> str:
    message = b"password-reset-token:" + seed
    digest = hmac.digest(secret_key.get_secret_value().encode(), message, "sha256")
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def save_reset_token(session: AsyncSession, email: str) -> uuid.UUID:
    seed = secrets.token_bytes(32)
    expires_at = datetime.now(UTC) + RESET_LIFETIME
    statement = (
        insert(ResetToken)
        .values(email=email, seed=seed, expires_at=expires_at)
        .on_conflict_do_update(
            constraint="uq_auth_reset_tokens_email",
            set_={
                "user_id": None,
                "seed": seed,
                "token_hash": None,
                "expires_at": expires_at,
                "updated_at": func.statement_timestamp(),
            },
        )
        .returning(ResetToken.id)
    )
    return (await session.execute(statement)).scalar_one()


async def request_password_reset(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    client_ip: str | None,
    data: PasswordReset,
) -> None:
    await count_reset_request(limiter, client_ip, data.email)
    reset_id = await save_reset_token(session, data.email)
    await jobs.defer(session, SendResetToken(reset_id=reset_id))


async def find_reset_token(
    session: AsyncSession, reset_id: uuid.UUID
) -> ResetToken | None:
    statement = (
        select(ResetToken)
        .where(ResetToken.id == reset_id, ResetToken.expires_at > datetime.now(UTC))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def issue_reset_token(
    session: AsyncSession, secret_key: SecretStr, reset: ResetToken
) -> str | None:
    user = await users.find_user_by_email(session, reset.email)
    if user is None or not user.active:
        return None
    token = derive_token(secret_key, reset.seed)
    reset.user_id = user.id
    reset.token_hash = hash_token(token)
    await flush(session, {})
    return token


async def use_reset_token(session: AsyncSession, token: str) -> users.UserDTO:
    statement = (
        delete(ResetToken)
        .where(
            ResetToken.token_hash == hash_token(token),
            ResetToken.expires_at > datetime.now(UTC),
        )
        .returning(ResetToken.user_id, ResetToken.email)
    )
    spent = (await session.execute(statement)).one_or_none()
    if spent is None:
        raise InvalidTokenError
    user_id, email = spent
    user = await users.find_user_by_email(session, email)
    if user is None or user.id != user_id or not user.active:
        raise InvalidTokenError
    return user


async def confirm_password_reset(
    session: AsyncSession,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    hashing_slots: asyncio.Semaphore,
    client_ip: str | None,
    data: PasswordResetConfirm,
) -> None:
    await count_reset_confirmation(limiter, client_ip)
    async with hashing_slots:
        user = await use_reset_token(session, data.token)
        password = PasswordSet(password=data.password)
        await set_password(session, hashing, user, password)
    audit.record(
        session,
        "auth.password_reset",
        actor_id=user.id,
        target_id=user.id,
        client_ip=client_ip,
        changes={},
    )


async def change_password(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    principal: Principal,
    client_ip: str | None,
    data: PasswordChange,
) -> None:
    user = await users.get_user(session, principal.user_id)
    await count_login_attempt(limiter, client_ip, user.email)
    await void_reset_tokens(session, user)
    await check_password(
        session, hashing, user.id, data.current_password, for_update=True
    )
    password = PasswordSet(password=data.new_password)
    await set_password(session, hashing, user, password)
    audit.record(
        session,
        "auth.password_changed",
        actor_id=user.id,
        target_id=user.id,
        client_ip=client_ip,
        changes={},
    )
    await jobs.defer(session, SendPasswordChangedNotice(user_id=user.id))
