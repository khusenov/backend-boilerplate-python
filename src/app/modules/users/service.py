import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import flush
from app.core.pagination import Page, PageQuery, decode_cursor, encode_cursor
from app.modules.users.errors import EmailTakenError, UserNotFoundError
from app.modules.users.models import User, UserStatus
from app.modules.users.schemas import UserCreate


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    user = User(email=data.email, name=data.name)
    session.add(user)
    await flush(session, {"uq_users_email": EmailTakenError})
    return user


async def get_user(
    session: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False
) -> User:
    user = await session.get(
        User, user_id, with_for_update=for_update, populate_existing=for_update
    )
    if user is None:
        raise UserNotFoundError
    return user


async def list_users(session: AsyncSession, query: PageQuery) -> Page[User]:
    statement = select(User).order_by(User.id.desc()).limit(query.limit + 1)
    if query.cursor is not None:
        statement = statement.where(User.id < decode_cursor(query.cursor))
    users = list(await session.scalars(statement))
    if len(users) <= query.limit:
        return Page(items=users, next_cursor=None)
    items = users[: query.limit]
    return Page(items=items, next_cursor=encode_cursor(items[-1].id))


async def deactivate_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await get_user(session, user_id, for_update=True)
    user.status = UserStatus.DEACTIVATED
    user.email = f"{user.id}@deactivated"
    user.name = ""
    await flush(session, {})
    return user
