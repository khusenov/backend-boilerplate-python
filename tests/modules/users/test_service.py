import uuid
from collections.abc import Awaitable, Callable

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageQuery
from app.core.security import Principal, Role
from app.modules.users import service
from app.modules.users.errors import EmailTakenError, UserNotFoundError
from app.modules.users.models import User, UserStatus
from app.modules.users.schemas import ProfileUpdate, UserCreate, UserUpdate
from tests.audit import audit_rows
from tests.factories import make_user, principal_for
from tests.statements import first_words, record_statements

ADMIN = Principal(user_id=uuid.uuid7(), session_id=uuid.uuid7(), role=Role.ADMIN)

type Write = Callable[[AsyncSession, User], Awaitable[User]]


async def deactivate(session: AsyncSession, user: User) -> User:
    return await service.deactivate_user(session, ADMIN, None, user.id)


async def promote(session: AsyncSession, user: User) -> User:
    return await service.update_user(
        session, ADMIN, None, user.id, UserUpdate(role=Role.ADMIN)
    )


async def rename(session: AsyncSession, user: User) -> User:
    return await service.update_profile(
        session, principal_for(user), ProfileUpdate(name="Grace Hopper")
    )


async def test_new_users_are_active_members_with_a_normalized_email(
    session: AsyncSession,
) -> None:
    data = UserCreate(email=" Ada@Example.com ", name=" Ada Lovelace ")

    user = await service.create_user(session, data)

    assert (user.email, user.name, user.role, user.status) == (
        "ada@example.com",
        "Ada Lovelace",
        Role.MEMBER,
        UserStatus.ACTIVE,
    )
    assert user.id.version == 7


async def test_a_duplicate_email_is_taken(session: AsyncSession) -> None:
    await make_user(session, email="ada@example.com")
    data = UserCreate(email="ADA@example.com", name="Ada Lovelace")

    with pytest.raises(EmailTakenError):
        await service.create_user(session, data)


async def test_a_missing_user_is_not_found(session: AsyncSession) -> None:
    await make_user(session)

    with pytest.raises(UserNotFoundError):
        await service.get_user(session, ADMIN, uuid.uuid7())


async def test_members_see_only_themselves(session: AsyncSession) -> None:
    member = await make_user(session)
    other = await make_user(session)
    viewer = principal_for(member)

    page = await service.list_users(session, viewer, PageQuery())

    assert page.items == [member]
    assert await service.get_user(session, viewer, member.id) is member
    with pytest.raises(UserNotFoundError):
        await service.get_user(session, viewer, other.id)


async def test_deactivated_users_leave_every_view(session: AsyncSession) -> None:
    kept = await make_user(session)
    gone = await make_user(session, status=UserStatus.DEACTIVATED)

    page = await service.list_users(session, ADMIN, PageQuery())

    assert page.items == [kept]
    with pytest.raises(UserNotFoundError):
        await service.get_user(session, ADMIN, gone.id)
    with pytest.raises(UserNotFoundError):
        await service.deactivate_user(session, ADMIN, None, gone.id)


async def test_pages_end_where_the_next_one_starts(session: AsyncSession) -> None:
    oldest, middle, newest = [await make_user(session) for _ in range(3)]

    first = await service.list_users(session, ADMIN, PageQuery(limit=2))
    second = await service.list_users(
        session, ADMIN, PageQuery(limit=2, cursor=first.next_cursor)
    )

    assert first.items == [newest, middle]
    assert second.items == [oldest]
    assert second.next_cursor is None


async def test_a_full_last_page_has_no_next_cursor(session: AsyncSession) -> None:
    users = [await make_user(session) for _ in range(2)]

    page = await service.list_users(session, ADMIN, PageQuery(limit=2))

    assert page.items == users[::-1]
    assert page.next_cursor is None


async def test_role_changes_are_audited_once(session: AsyncSession) -> None:
    user = await make_user(session)
    promotion = UserUpdate(role=Role.ADMIN)

    await service.update_user(session, ADMIN, "203.0.113.7", user.id, promotion)
    await service.update_user(session, ADMIN, "203.0.113.7", user.id, promotion)

    assert user.role == Role.ADMIN
    assert await audit_rows(session) == [
        (
            ADMIN.user_id,
            "users.role_changed",
            user.id,
            "203.0.113.7",
            None,
            {"role": "admin"},
        )
    ]


async def test_deactivation_anonymizes_the_user(session: AsyncSession) -> None:
    user = await make_user(session, email="ada@example.com", name="Ada Lovelace")

    await service.deactivate_user(session, ADMIN, "203.0.113.7", user.id)

    assert (user.email, user.name, user.status) == (
        f"{user.id}@deactivated",
        "",
        UserStatus.DEACTIVATED,
    )
    assert user.updated_at > user.created_at
    assert await audit_rows(session) == [
        (
            ADMIN.user_id,
            "users.deactivated",
            user.id,
            "203.0.113.7",
            None,
            {"status": "deactivated"},
        )
    ]


@pytest.mark.parametrize(
    ("write", "writes"),
    [
        (deactivate, ["INSERT INTO audit_events", "UPDATE users SET"]),
        (promote, ["INSERT INTO audit_events", "UPDATE users SET"]),
        (rename, ["UPDATE users SET"]),
    ],
    ids=["deactivate", "promote", "rename"],
)
async def test_writes_lock_the_row_before_changing_it(
    session: AsyncSession, write: Write, writes: list[str]
) -> None:
    user = await make_user(session)
    statements = await record_statements(session)

    await write(session, user)

    lock, *changes = statements
    assert lock.endswith(" FOR UPDATE")
    assert sorted(map(first_words, changes)) == writes


async def test_locking_reads_refresh_the_row(session: AsyncSession) -> None:
    user = await make_user(session, name="Ada Lovelace")
    await session.execute(
        update(User).where(User.id == user.id).values(name="Grace Hopper"),
        execution_options={"synchronize_session": False},
    )

    locked = await service.get_user(session, ADMIN, user.id, for_update=True)

    assert locked.name == "Grace Hopper"


async def test_deactivation_frees_the_email(session: AsyncSession) -> None:
    user = await make_user(session, email="ada@example.com")
    await service.deactivate_user(session, ADMIN, None, user.id)
    data = UserCreate(email="ada@example.com", name="Ada Lovelace")

    await service.create_user(session, data)


async def test_no_account_can_claim_a_tombstone(session: AsyncSession) -> None:
    user = await make_user(session)
    await service.deactivate_user(session, ADMIN, None, user.id)

    with pytest.raises(ValidationError):
        UserCreate(email=user.email, name="Ada Lovelace")


async def test_creating_an_admin_is_one_upsert(session: AsyncSession) -> None:
    statements = await record_statements(session)

    await service.create_admin(session, UserCreate(email="ada@example.com", name="Ada"))

    assert list(map(first_words, statements)) == ["INSERT INTO users"]
    assert " ON CONFLICT ON CONSTRAINT uq_users_email DO UPDATE SET " in statements[0]


async def test_creating_an_admin_twice_keeps_one_row(session: AsyncSession) -> None:
    data = UserCreate(email="Ada@Example.com", name="Ada")

    first = await service.create_admin(session, data)
    second = await service.create_admin(
        session, UserCreate(email="ada@example.com", name="Ada Lovelace")
    )

    users = list(await session.scalars(select(User)))
    assert users == [first]
    assert second is first
    assert (first.email, first.name, first.role) == (
        "ada@example.com",
        "Ada Lovelace",
        Role.ADMIN,
    )


async def test_creating_an_admin_promotes_an_existing_member(
    session: AsyncSession,
) -> None:
    member = await make_user(session, email="ada@example.com")
    before = member.updated_at

    admin = await service.create_admin(
        session, UserCreate(email="ada@example.com", name="Ada Lovelace")
    )

    assert admin is member
    assert member.role == Role.ADMIN
    assert member.updated_at > before


async def test_unknown_statuses_are_refused(session: AsyncSession) -> None:
    with pytest.raises(StatementError, match="'retired' is not among"):
        await make_user(session, status="retired")
