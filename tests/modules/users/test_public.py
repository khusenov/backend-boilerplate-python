import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Role
from app.modules.users import public as users
from app.modules.users.errors import UserNotFoundError
from tests.factories import make_user


@pytest.mark.parametrize(
    ("status", "active"), [("active", True), ("deactivated", False)]
)
async def test_other_modules_read_users_as_dtos(
    session: AsyncSession, status: str, active: bool
) -> None:
    user = await make_user(session, email="ada@example.com", status=status)

    found = await users.get_user(session, user.id)

    assert found == users.UserDTO(
        id=user.id,
        email="ada@example.com",
        role=Role.MEMBER,
        active=active,
    )


async def test_a_missing_user_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(UserNotFoundError):
        await users.get_user(session, uuid.uuid7())


async def test_other_modules_find_users_by_email(session: AsyncSession) -> None:
    user = await make_user(session, email="ada@example.com", role=Role.ADMIN)

    found = await users.find_user_by_email(session, "ada@example.com")
    missing = await users.find_user_by_email(session, "bob@example.com")

    assert found == users.UserDTO(
        id=user.id,
        email="ada@example.com",
        role=Role.ADMIN,
        active=True,
    )
    assert missing is None
