import secrets
import uuid
from datetime import UTC, datetime
from itertools import count

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.modules.auth.models import Credential, LoginSession
from app.modules.auth.service import PASSWORD_HASHER, SESSION_LIFETIME, hash_token
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


def principal_for(user: User) -> Principal:
    return Principal(user_id=user.id, session_id=uuid.uuid7(), role=user.role)
