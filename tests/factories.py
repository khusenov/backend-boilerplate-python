import secrets
import uuid
from datetime import UTC, datetime
from itertools import count

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base
from app.core.security import Principal
from app.modules.auth.models import Credential, LoginSession
from app.modules.auth.service.credentials import PASSWORD_HASHER, hash_token
from app.modules.auth.service.passwords import (
    find_reset_token,
    issue_reset_token,
    save_reset_token,
)
from app.modules.auth.service.sessions import SESSION_LIFETIME
from app.modules.auth.service.verification import (
    find_verification_code,
    issue_code,
    save_verification_code,
)
from app.modules.users.models import User

PASSWORD = "correct horse battery staple"

numbers = count(1)


async def make_user(session: AsyncSession, **overrides: object) -> User:
    number = next(numbers)
    fields = {"email": f"user{number}@example.com", "name": f"User {number}"}
    user = User(**fields | overrides)
    session.add(user)
    await session.flush()
    return user


async def make_credential(
    session: AsyncSession, user: User, **overrides: object
) -> Credential:
    fields = {"user_id": user.id, "password_hash": PASSWORD_HASHER.hash(PASSWORD)}
    credential = Credential(**fields | overrides)
    session.add(credential)
    await session.flush()
    return credential


async def make_login_session(
    session: AsyncSession, user: User, **overrides: object
) -> str:
    token = secrets.token_urlsafe(32)
    fields = {
        "user_id": user.id,
        "token_hash": hash_token(token),
        "expires_at": datetime.now(UTC) + SESSION_LIFETIME,
    }
    session.add(LoginSession(**fields | overrides))
    await session.flush()
    return token


async def make_code(
    session: AsyncSession, secret_key: SecretStr, email: str, **overrides: object
) -> str:
    code_id = await save_verification_code(session, email, user_id=None)
    verification = await find_verification_code(session, code_id)
    assert verification is not None
    code = await issue_code(session, secret_key, verification)
    assert code is not None
    for name, value in overrides.items():
        setattr(verification, name, value)
    await session.flush()
    return code


async def make_reset_token(
    session: AsyncSession, secret_key: SecretStr, user: User, **overrides: object
) -> str:
    reset_id = await save_reset_token(session, user.email)
    reset = await find_reset_token(session, reset_id)
    assert reset is not None
    token = await issue_reset_token(session, secret_key, reset)
    assert token is not None
    for name, value in overrides.items():
        setattr(reset, name, value)
    await session.flush()
    return token


def wrong_code(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def principal_for(user: User) -> Principal:
    return Principal(user_id=user.id, session_id=uuid.uuid7(), role=user.role)


async def count_rows(session: AsyncSession, model: type[Base]) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()
