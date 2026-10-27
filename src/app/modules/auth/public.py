from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.modules.auth import service as _service

__all__ = ["authenticate"]


async def authenticate(session: AsyncSession, token: str) -> Principal | None:
    return await _service.authenticate(session, token)
