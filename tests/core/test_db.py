from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import psycopg_url
from tests.scratch import ScratchNote


@pytest.fixture
async def session(
    scratch_tables: None, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2"])
def test_database_urls_use_psycopg(scheme: str) -> None:
    raw = f"{scheme}://app:secret@db.example.com:5432/app?sslmode=require"

    url = psycopg_url(SecretStr(raw))

    assert url.render_as_string(hide_password=False) == (
        "postgresql+psycopg://app:secret@db.example.com:5432/app?sslmode=require"
    )
    assert "secret" not in str(url)


def test_ids_come_first_and_timestamps_last() -> None:
    columns = [column.name for column in ScratchNote.__table__.columns]

    assert columns == ["id", "body", "created_at", "updated_at"]


async def test_rows_get_uuid7_ids_and_statement_timestamps(
    session: AsyncSession,
) -> None:
    first = ScratchNote(body="first")
    session.add(first)
    await session.flush()
    second = ScratchNote(body="second")
    session.add(second)
    await session.flush()

    assert [first.id.version, second.id.version] == [7, 7]
    assert first.created_at == first.updated_at
    assert first.created_at.tzinfo is not None
    assert second.created_at > first.created_at


async def test_updates_return_the_new_updated_at(session: AsyncSession) -> None:
    note = ScratchNote(body="draft")
    session.add(note)
    await session.flush()

    note.body = "final"
    await session.flush()

    assert note.updated_at > note.created_at


async def test_flushes_insert_python_made_ids_in_one_statement(
    session: AsyncSession,
) -> None:
    connection = await session.connection()
    statements: list[str] = []

    @event.listens_for(connection.sync_connection, "before_cursor_execute", named=True)
    def record(statement: str, **_: object) -> None:
        statements.append(statement)

    session.add_all([ScratchNote(body="first"), ScratchNote(body="second")])
    await session.flush()

    assert [statement.partition(" VALUES ")[0] for statement in statements] == [
        "INSERT INTO scratch_notes (id, body)"
    ]


async def test_sql_inserts_get_uuid7_ids_from_postgres(session: AsyncSession) -> None:
    note_id = await session.scalar(
        text("INSERT INTO scratch_notes (body) VALUES ('raw') RETURNING id")
    )

    assert note_id.version == 7


async def test_constraints_are_named_by_the_convention(session: AsyncSession) -> None:
    constraints = await session.scalars(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'scratch_tags'::regclass AND contype <> 'n' "
            "ORDER BY conname"
        )
    )
    indexes = await session.scalars(
        text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'scratch_tags' "
            "ORDER BY indexname"
        )
    )

    assert list(constraints) == [
        "ck_scratch_tags_name_not_empty",
        "fk_scratch_tags_note_id_scratch_notes",
        "pk_scratch_tags",
        "uq_scratch_tags_note_id_name",
    ]
    assert list(indexes) == [
        "ix_scratch_tags_name_created_at",
        "ix_scratch_tags_note_id",
        "pk_scratch_tags",
        "uq_scratch_tags_note_id_name",
    ]
