import asyncio
import hmac
import secrets
import uuid
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta

from limits.aio.strategies import MovingWindowRateLimiter
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import flush
from app.core.errors import ConflictError
from app.core.jobs.queue import JobQueue
from app.core.security import Principal
from app.modules.auth.errors import InvalidCodeError
from app.modules.auth.models import VerificationCode
from app.modules.auth.schemas import (
    EmailChange,
    EmailChangeConfirm,
    PasswordSet,
    SendCode,
    Signup,
    SignupConfirm,
)
from app.modules.auth.service.credentials import (
    check_password,
    set_password,
    void_reset_tokens,
)
from app.modules.auth.service.limits import (
    count_code_request,
    count_guess,
    count_login_attempt,
)
from app.modules.users import public as users

CODE_LIFETIME = timedelta(minutes=15)


def code_mac(secret_key: SecretStr, email: str, code: str) -> bytes:
    message = f"verify-email:{email}:{code}".encode()
    return hmac.digest(secret_key.get_secret_value().encode(), message, "sha256")


def derive_code(secret_key: SecretStr, seed: bytes) -> str:
    message = b"verify-email-code:" + seed
    digest = hmac.digest(secret_key.get_secret_value().encode(), message, "sha256")
    return f"{int.from_bytes(digest) % 1_000_000:06d}"


async def save_verification_code(
    session: AsyncSession, email: str, *, user_id: uuid.UUID | None
) -> uuid.UUID:
    seed = secrets.token_bytes(32)
    expires_at = datetime.now(UTC) + CODE_LIFETIME
    statement = (
        insert(VerificationCode)
        .values(email=email, user_id=user_id, seed=seed, expires_at=expires_at)
        .on_conflict_do_update(
            constraint="uq_auth_verification_codes_email",
            set_={
                "user_id": user_id,
                "seed": seed,
                "code_mac": None,
                "expires_at": expires_at,
                "updated_at": func.statement_timestamp(),
            },
        )
        .returning(VerificationCode.id)
    )
    return (await session.execute(statement)).scalar_one()


async def request_code(
    session: AsyncSession, jobs: JobQueue, email: str, *, user_id: uuid.UUID | None
) -> None:
    code_id = await save_verification_code(session, email, user_id=user_id)
    await jobs.defer(session, SendCode(code_id=code_id))


async def find_verification_code(
    session: AsyncSession, code_id: uuid.UUID
) -> VerificationCode | None:
    statement = (
        select(VerificationCode)
        .where(
            VerificationCode.id == code_id,
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def issue_code(
    session: AsyncSession, secret_key: SecretStr, verification: VerificationCode
) -> str | None:
    if await users.find_user_by_email(session, verification.email) is not None:
        return None
    code = derive_code(secret_key, verification.seed)
    verification.code_mac = code_mac(secret_key, verification.email, code)
    await flush(session, {})
    return code


async def use_code(
    session: AsyncSession,
    secret_key: SecretStr,
    email: str,
    *,
    user_id: uuid.UUID | None,
    code: str,
) -> None:
    statement = (
        select(VerificationCode)
        .where(
            VerificationCode.email == email,
            VerificationCode.user_id == user_id,
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    verification = await session.scalar(statement)
    expected = code_mac(secret_key, email, code)
    if (
        verification is None
        or verification.code_mac is None
        or not hmac.compare_digest(verification.code_mac, expected)
    ):
        raise InvalidCodeError
    await session.delete(verification)


async def request_signup(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    client_ip: str | None,
    data: Signup,
) -> None:
    await count_code_request(limiter, client_ip, data.email)
    await request_code(session, jobs, data.email, user_id=None)


async def confirm_signup(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    hashing_slots: asyncio.Semaphore,
    secret_key: SecretStr,
    client_ip: str | None,
    data: SignupConfirm,
) -> None:
    await count_guess(limiter, client_ip, data.email)
    async with hashing_slots:
        await use_code(session, secret_key, data.email, user_id=None, code=data.code)
        try:
            user = await users.create_user(session, jobs, data.email, data.name)
        except ConflictError:
            raise InvalidCodeError from None
        password = PasswordSet(password=data.password)
        await set_password(session, hashing, user, password)


async def request_email_change(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    hashing: Executor,
    principal: Principal,
    client_ip: str | None,
    data: EmailChange,
) -> None:
    user = await users.get_user(session, principal.user_id)
    await count_login_attempt(limiter, client_ip, user.email)
    await count_code_request(limiter, client_ip, data.email)
    await check_password(session, hashing, user.id, data.password)
    await request_code(session, jobs, data.email, user_id=user.id)


async def confirm_email_change(
    session: AsyncSession,
    jobs: JobQueue,
    limiter: MovingWindowRateLimiter,
    secret_key: SecretStr,
    principal: Principal,
    client_ip: str | None,
    data: EmailChangeConfirm,
) -> None:
    await count_guess(limiter, client_ip, data.email)
    await use_code(
        session, secret_key, data.email, user_id=principal.user_id, code=data.code
    )
    try:
        await users.change_email(session, jobs, principal, client_ip, data.email)
    except ConflictError:
        raise InvalidCodeError from None
    user = await users.get_user(session, principal.user_id)
    await void_reset_tokens(session, user)
