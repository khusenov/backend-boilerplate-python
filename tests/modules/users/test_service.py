import uuid
from collections.abc import Awaitable, Callable

import pytest
from pydantic import ValidationError
from sqlalchemy import event, update
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageQuery
from app.modules.users import service
from app.modules.users.errors import EmailTakenError, UserNotFoundError
from app.modules.users.models import User, UserStatus
from app.modules.users.schemas import UserCreate
from tests.factories import make_user

type UseCase = Callable[[AsyncSession, uuid.UUID], Awaitable[User]]


async def test_new_users_are_active_with_a_normalized_email(
    session: AsyncSession,
) -> None:
    data = UserCreate(email=" Ada@Example.com ", name=" Ada Lovelace ")

    user = await service.create_user(session, data)

    assert (user.email, user.name, user.status) == (
        "ada@example.com",
        "Ada Lovelace",
        UserStatus.ACTIVE,
    )
    assert user.id.version == 7


async def test_a_duplicate_email_is_taken(session: AsyncSession) -> None:
    await make_user(session, email="ada@example.com")
    data = UserCreate(email="ADA@example.com", name="Ada Lovelace")

    with pytest.raises(EmailTakenError):
        await service.create_user(session, data)


@pytest.mark.parametrize("use_case", [service.get_user, service.deactivate_user])
async def test_a_missing_user_is_not_found(
    session: AsyncSession, use_case: UseCase
) -> None:
    await make_user(session)

    with pytest.raises(UserNotFoundError):
        await use_case(session, uuid.uuid7())


async def test_pages_end_where_the_next_one_starts(session: AsyncSession) -> None:
    oldest, middle, newest = [await make_user(session) for _ in range(3)]

    first = await service.list_users(session, PageQuery(limit=2))
    second = await service.list_users(
        session, PageQuery(limit=2, cursor=first.next_cursor)
    )

    assert first.items == [newest, middle]
    assert second.items == [oldest]
    assert second.next_cursor is None


async def test_a_full_last_page_has_no_next_cursor(session: AsyncSession) -> None:
    users = [await make_user(session) for _ in range(2)]

    page = await service.list_users(session, PageQuery(limit=2))

    assert page.items == users[::-1]
    assert page.next_cursor is None


async def test_deactivation_anonymizes_the_user(session: AsyncSession) -> None:
    user = await make_user(session, email="ada@example.com", name="Ada Lovelace")

    await service.deactivate_user(session, user.id)

    assert (user.email, user.name, user.status) == (
        f"{user.id}@deactivated",
        "",
        UserStatus.DEACTIVATED,
    )
    assert user.updated_at > user.created_at


async def test_deactivation_locks_the_row_before_changing_it(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    connection = await session.connection()
    statements: list[str] = []

    @event.listens_for(connection.sync_connection, "before_cursor_execute", named=True)
    def record(statement: str, **_: object) -> None:
        statements.append(statement)

    await service.deactivate_user(session, user.id)

    assert [statement.split()[0] for statement in statements] == ["SELECT", "UPDATE"]
    assert statements[0].endswith(" FOR UPDATE")


async def test_locking_reads_refresh_the_row(session: AsyncSession) -> None:
    user = await make_user(session, name="Ada Lovelace")
    await session.execute(
        update(User).where(User.id == user.id).values(name="Grace Hopper"),
        execution_options={"synchronize_session": False},
    )

    locked = await service.get_user(session, user.id, for_update=True)

    assert locked.name == "Grace Hopper"


async def test_deactivation_frees_the_email(session: AsyncSession) -> None:
    user = await make_user(session, email="ada@example.com")
    await service.deactivate_user(session, user.id)
    data = UserCreate(email="ada@example.com", name="Ada Lovelace")

    await service.create_user(session, data)


async def test_no_account_can_claim_a_tombstone(session: AsyncSession) -> None:
    user = await make_user(session)
    await service.deactivate_user(session, user.id)

    with pytest.raises(ValidationError):
        UserCreate(email=user.email, name="Ada Lovelace")


async def test_unknown_statuses_are_refused(session: AsyncSession) -> None:
    with pytest.raises(StatementError, match="'retired' is not among"):
        await make_user(session, status="retired")
