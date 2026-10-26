from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True, kw_only=True)
class Resources:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    async def close(self) -> None:
        await self.engine.dispose()
