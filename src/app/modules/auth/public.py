from concurrent.futures import Executor

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.modules.auth import schemas as _schemas
from app.modules.auth import service as _service
from app.modules.users import public as _users

__all__ = ["authenticate", "create_admin"]


async def authenticate(session: AsyncSession, token: str) -> Principal | None:
    return await _service.authenticate(session, token)


async def create_admin(
    session: AsyncSession, hashing: Executor, email: str, name: str, password: str
) -> _users.UserDTO:
    data = _schemas.PasswordSet(password=password)
    return await _service.create_admin(session, hashing, email, name, data)
