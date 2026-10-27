import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass

from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.jobs.queue import JobQueue
from app.core.security import Authenticate


@dataclass(frozen=True, slots=True, kw_only=True)
class Resources:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    jobs: JobQueue
    limiter: MovingWindowRateLimiter
    hashing: Executor
    hashing_slots: asyncio.Semaphore
    authenticate: Authenticate

    async def close(self) -> None:
        self.hashing.shutdown()
        await self.engine.dispose()
