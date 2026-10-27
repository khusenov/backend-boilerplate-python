import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import Executor
from typing import Annotated, TypedDict, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotAuthenticatedError, UntrustedOriginError
from app.core.jobs.queue import JobQueue
from app.core.resources import Resources
from app.core.security import Principal

SESSION_COOKIE = "__Host-session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

bearer_token = HTTPBearer(scheme_name="bearer", auto_error=False)
session_cookie = APIKeyCookie(
    name=SESSION_COOKIE, scheme_name="session", auto_error=False
)


class AppState(TypedDict):
    settings: Settings
    resources: Resources


def get_settings(request: Request) -> Settings:
    return cast(AppState, request.state)["settings"]


def get_resources(request: Request) -> Resources:
    return cast(AppState, request.state)["resources"]


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_resources(request).sessionmaker.begin() as session:
        if request.method in SAFE_METHODS:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session, scope="function")]


async def get_jobs(request: Request) -> JobQueue:
    return get_resources(request).jobs


Jobs = Annotated[JobQueue, Depends(get_jobs)]


async def get_limiter(request: Request) -> MovingWindowRateLimiter:
    return get_resources(request).limiter


Limiter = Annotated[MovingWindowRateLimiter, Depends(get_limiter)]


async def get_hashing(request: Request) -> Executor:
    return get_resources(request).hashing


Hashing = Annotated[Executor, Depends(get_hashing)]


async def get_hashing_slots(request: Request) -> asyncio.Semaphore:
    return get_resources(request).hashing_slots


HashingSlots = Annotated[asyncio.Semaphore, Depends(get_hashing_slots)]


async def get_client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


ClientIp = Annotated[str | None, Depends(get_client_ip)]


async def require_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    trusted = get_settings(request).cors_origins
    if request.method not in SAFE_METHODS and origin not in trusted:
        raise UntrustedOriginError


async def current_principal(
    request: Request,
    session: DbSession,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_token)],
    cookie: Annotated[str | None, Depends(session_cookie)],
) -> Principal:
    if bearer is not None:
        token = bearer.credentials
    elif cookie is not None:
        await require_trusted_origin(request)
        token = cookie
    else:
        raise NotAuthenticatedError
    principal = await get_resources(request).authenticate(session, token)
    if principal is None:
        raise NotAuthenticatedError
    structlog.contextvars.bind_contextvars(user_id=str(principal.user_id))
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
