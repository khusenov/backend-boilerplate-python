import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users import service as _service
from app.modules.users.schemas import UserDTO

__all__ = ["UserDTO", "get_user"]


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserDTO:
    user = await _service.get_user(session, user_id)
    return UserDTO.model_validate(user)
