import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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

    assert found == users.UserDTO(id=user.id, email="ada@example.com", active=active)


async def test_a_missing_user_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(UserNotFoundError):
        await users.get_user(session, uuid.uuid7())
