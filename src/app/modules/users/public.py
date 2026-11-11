import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs.queue import JobQueue
from app.core.security import Principal
from app.modules.users import schemas as _schemas
from app.modules.users import service as _service
from app.modules.users.schemas import UserDTO

__all__ = [
    "UserDTO",
    "change_email",
    "create_admin",
    "create_user",
    "find_user_by_email",
    "get_user",
]


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserDTO:
    user = await _service.get_user_unscoped(session, user_id)
    return UserDTO.model_validate(user)


async def find_user_by_email(session: AsyncSession, email: str) -> UserDTO | None:
    user = await _service.find_user_by_email(session, email)
    return None if user is None else UserDTO.model_validate(user)


async def create_user(
    session: AsyncSession, jobs: JobQueue, email: str, name: str
) -> UserDTO:
    data = _schemas.UserCreate(email=email, name=name)
    user = await _service.create_user(session, jobs, data)
    return UserDTO.model_validate(user)


async def create_admin(session: AsyncSession, email: str, name: str) -> UserDTO:
    data = _schemas.UserCreate(email=email, name=name)
    user = await _service.create_admin(session, data)
    return UserDTO.model_validate(user)


async def change_email(
    session: AsyncSession,
    jobs: JobQueue,
    principal: Principal,
    client_ip: str | None,
    email: str,
) -> None:
    data = _schemas.EmailUpdate(email=email)
    await _service.change_email(session, jobs, principal, client_ip, data)
