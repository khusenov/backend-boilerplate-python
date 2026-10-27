from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


async def record_statements(session: AsyncSession) -> list[str]:
    statements: list[str] = []
    connection = await session.connection()

    @event.listens_for(connection.sync_connection, "before_cursor_execute", named=True)
    def record(statement: str, **_: object) -> None:
        statements.append(" ".join(statement.split()))

    return statements


def first_words(statement: str) -> str:
    return " ".join(statement.split()[:3])
