import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.http.authorization import require
from app.core.http.deps import ClientIp, CurrentPrincipal, DbSession, Jobs
from app.core.pagination import PageQuery
from app.core.security import Permission
from app.modules.users import service
from app.modules.users.schemas import (
    ProfileUpdate,
    UserCreate,
    UserPage,
    UserRead,
    UserUpdate,
)

router = APIRouter(prefix="/users")


@router.get("", dependencies=[require(Permission.USERS_READ)])
async def list_users(
    query: Annotated[PageQuery, Query()],
    principal: CurrentPrincipal,
    session: DbSession,
) -> UserPage:
    page = await service.list_users(session, principal, query)
    return UserPage.model_validate(page)


@router.post("", status_code=201, dependencies=[require(Permission.USERS_WRITE)])
async def create_user(body: UserCreate, session: DbSession, jobs: Jobs) -> UserRead:
    user = await service.create_user(session, jobs, body)
    return UserRead.model_validate(user)


@router.get("/me", dependencies=[require(Permission.USERS_READ)])
async def get_current_user(principal: CurrentPrincipal, session: DbSession) -> UserRead:
    user = await service.get_user(session, principal, principal.user_id)
    return UserRead.model_validate(user)


@router.patch("/me", dependencies=[require(Permission.USERS_WRITE_SELF)])
async def update_current_user(
    body: ProfileUpdate, principal: CurrentPrincipal, session: DbSession
) -> UserRead:
    user = await service.update_profile(session, principal, body)
    return UserRead.model_validate(user)


@router.get("/{user_id}", dependencies=[require(Permission.USERS_READ)])
async def get_user(
    user_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> UserRead:
    user = await service.get_user(session, principal, user_id)
    return UserRead.model_validate(user)


@router.patch("/{user_id}", dependencies=[require(Permission.USERS_WRITE)])
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    principal: CurrentPrincipal,
    session: DbSession,
    client_ip: ClientIp,
) -> UserRead:
    user = await service.update_user(session, principal, client_ip, user_id, body)
    return UserRead.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=204,
    response_class=Response,
    dependencies=[require(Permission.USERS_WRITE)],
)
async def deactivate_user(
    user_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    client_ip: ClientIp,
) -> None:
    await service.deactivate_user(session, principal, client_ip, user_id)
