from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.ext.asyncio import AsyncSession


async def record_statements(session: AsyncSession) -> list[str]:
    statements: list[str] = []
    connection = await session.connection()

    @event.listens_for(connection.sync_connection, "before_cursor_execute", named=True)
    def record(statement: str, **_: object) -> None:
        statements.append(" ".join(statement.split()))

    return statements


async def record_deletions(session: AsyncSession) -> list[int]:
    deletions: list[int] = []
    connection = await session.connection()

    @event.listens_for(connection.sync_connection, "after_cursor_execute", named=True)
    def record(statement: str, cursor: DBAPICursor, **_: object) -> None:
        if statement.startswith("DELETE"):
            deletions.append(cursor.rowcount)

    return deletions


def first_words(statement: str) -> str:
    return " ".join(statement.split()[:3])
