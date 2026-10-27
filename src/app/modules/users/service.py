import uuid

from pydantic import MISSING
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import flush
from app.core.pagination import Page, PageQuery, decode_cursor, encode_cursor
from app.core.security import Permission, Principal, Role
from app.modules.users.errors import EmailTakenError, UserNotFoundError
from app.modules.users.models import User, UserStatus
from app.modules.users.schemas import ProfileUpdate, UserCreate, UserUpdate


def visible_users(principal: Principal) -> Select[User]:
    statement = select(User).where(User.status == UserStatus.ACTIVE)
    if principal.can(Permission.USERS_READ_ALL):
        return statement
    return statement.where(User.id == principal.user_id)


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    user = User(email=data.email, name=data.name)
    session.add(user)
    await flush(session, {"uq_users_email": EmailTakenError})
    return user


async def create_admin(session: AsyncSession, data: UserCreate) -> User:
    statement = (
        insert(User)
        .values(email=data.email, name=data.name, role=Role.ADMIN)
        .on_conflict_do_update(
            constraint="uq_users_email",
            set_={
                "name": data.name,
                "role": Role.ADMIN,
                "updated_at": func.statement_timestamp(),
            },
        )
        .returning(User)
        .execution_options(populate_existing=True)
    )
    user = (await session.scalars(statement)).one()
    audit.record(
        session,
        "users.admin_created",
        actor_id=None,
        target_id=user.id,
        client_ip=None,
        changes={"role": Role.ADMIN},
    )
    return user


async def get_user(
    session: AsyncSession,
    principal: Principal,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> User:
    statement = visible_users(principal).where(User.id == user_id)
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    user = await session.scalar(statement)
    if user is None:
        raise UserNotFoundError
    return user


async def get_user_unscoped(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFoundError
    return user


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def list_users(
    session: AsyncSession, principal: Principal, query: PageQuery
) -> Page[User]:
    statement = visible_users(principal).order_by(User.id.desc()).limit(query.limit + 1)
    if query.cursor is not None:
        statement = statement.where(User.id < decode_cursor(query.cursor))
    users = list(await session.scalars(statement))
    if len(users) <= query.limit:
        return Page(items=users, next_cursor=None)
    items = users[: query.limit]
    return Page(items=items, next_cursor=encode_cursor(items[-1].id))


async def update_profile(
    session: AsyncSession, principal: Principal, data: ProfileUpdate
) -> User:
    user = await get_user(session, principal, principal.user_id, for_update=True)
    if data.name is not MISSING:
        user.name = data.name
    await flush(session, {})
    return user


async def update_user(
    session: AsyncSession,
    principal: Principal,
    client_ip: str | None,
    user_id: uuid.UUID,
    data: UserUpdate,
) -> User:
    user = await get_user(session, principal, user_id, for_update=True)
    if data.name is not MISSING:
        user.name = data.name
    if data.role is not MISSING and data.role != user.role:
        user.role = data.role
        audit.record(
            session,
            "users.role_changed",
            actor_id=principal.user_id,
            target_id=user.id,
            client_ip=client_ip,
            changes={"role": data.role},
        )
    await flush(session, {})
    return user


async def deactivate_user(
    session: AsyncSession,
    principal: Principal,
    client_ip: str | None,
    user_id: uuid.UUID,
) -> User:
    user = await get_user(session, principal, user_id, for_update=True)
    user.status = UserStatus.DEACTIVATED
    user.email = f"{user.id}@deactivated"
    user.name = ""
    audit.record(
        session,
        "users.deactivated",
        actor_id=principal.user_id,
        target_id=user.id,
        client_ip=client_ip,
        changes={"status": UserStatus.DEACTIVATED},
    )
    await flush(session, {})
    return user
