from collections.abc import AsyncIterator
from typing import Annotated, TypedDict, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resources import Resources


class AppState(TypedDict):
    resources: Resources


def get_resources(request: Request) -> Resources:
    return cast(AppState, request.state)["resources"]


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_resources(request).sessionmaker.begin() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session, scope="function")]
