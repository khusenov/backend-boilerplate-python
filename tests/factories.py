from itertools import count

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User

numbers = count(1)


async def make_user(session: AsyncSession, **overrides: object) -> User:
    number = next(numbers)
    fields = {"email": f"user{number}@example.com", "name": f"User {number}"}
    user = User(**fields | overrides)
    session.add(user)
    await session.flush()
    return user
