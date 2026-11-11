import asyncio
import hashlib
import secrets
import uuid
from concurrent.futures import Executor
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.errors import WrongPasswordError
from app.modules.auth.models import Credential, LoginSession, ResetToken
from app.modules.auth.schemas import PasswordSet
from app.modules.users import public as users

PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


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


async def check_password(
    session: AsyncSession,
    hashing: Executor,
    user_id: uuid.UUID,
    password: str,
    *,
    for_update: bool = False,
) -> None:
    statement = select(Credential.password_hash).where(Credential.user_id == user_id)
    if for_update:
        statement = statement.with_for_update()
    password_hash = await session.scalar(statement)
    if not await verify_password(hashing, password_hash, password):
        raise WrongPasswordError


async def void_reset_tokens(session: AsyncSession, user: users.UserDTO) -> None:
    await session.execute(
        delete(ResetToken).where(
            or_(ResetToken.user_id == user.id, ResetToken.email == user.email)
        )
    )


async def delete_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))


async def set_password(
    session: AsyncSession, hashing: Executor, user: users.UserDTO, data: PasswordSet
) -> None:
    password_hash = await hash_password(hashing, data.password)
    await void_reset_tokens(session, user)
    await session.execute(
        insert(Credential)
        .values(user_id=user.id, password_hash=password_hash)
        .on_conflict_do_update(
            constraint="uq_auth_credentials_user_id",
            set_={
                "password_hash": password_hash,
                "updated_at": func.statement_timestamp(),
            },
        )
    )
    await delete_sessions(session, user.id)


async def create_admin(
    session: AsyncSession, hashing: Executor, email: str, name: str, data: PasswordSet
) -> users.UserDTO:
    admin = await users.create_admin(session, email, name)
    await set_password(session, hashing, admin, data)
    return admin
