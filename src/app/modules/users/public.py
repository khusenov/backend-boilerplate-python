import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users import service as _service
from app.modules.users.schemas import UserDTO

__all__ = ["UserDTO", "find_user_by_email", "get_user"]


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserDTO:
    user = await _service.get_user(session, user_id)
    return UserDTO.model_validate(user)


async def find_user_by_email(session: AsyncSession, email: str) -> UserDTO | None:
    user = await _service.find_user_by_email(session, email)
    return None if user is None else UserDTO.model_validate(user)
