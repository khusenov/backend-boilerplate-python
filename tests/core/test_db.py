import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import conninfo, flush, psycopg_url
from app.core.errors import ConflictError
from tests.scratch import ScratchNote, ScratchTag


class TagTakenError(ConflictError):
    code = "tags.taken"


@pytest.fixture
def session(scratch_tables: None, session: AsyncSession) -> AsyncSession:
    return session


async def add_duplicate_tags(session: AsyncSession) -> None:
    note = ScratchNote(body="tagged")
    session.add(note)
    await session.flush()
    session.add_all([ScratchTag(note_id=note.id, name="draft") for _ in range(2)])


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2"])
def test_database_urls_use_psycopg(scheme: str) -> None:
    raw = f"{scheme}://app:secret@db.example.com:5432/app?sslmode=require"

    url = psycopg_url(SecretStr(raw))

    assert url.render_as_string(hide_password=False) == (
        "postgresql+psycopg://app:secret@db.example.com:5432/app?sslmode=require"
    )
    assert "secret" not in str(url)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_libpq_urls_drop_the_driver(scheme: str) -> None:
    raw = f"{scheme}://app:secret@db.example.com:5432/app?sslmode=require"

    assert conninfo(SecretStr(raw)) == (
        "postgresql://app:secret@db.example.com:5432/app?sslmode=require"
    )


def test_libpq_urls_keep_their_escapes() -> None:
    raw = (
        "postgresql+psycopg://app:p%20ss@db.example.com:5432/app"
        "?options=-c%20statement_timeout%3D7s"
    )

    assert conninfo_to_dict(conninfo(SecretStr(raw))) == {
        "user": "app",
        "password": "p ss",
        "host": "db.example.com",
        "port": "5432",
        "dbname": "app",
        "options": "-c statement_timeout=7s",
    }


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


async def test_flush_raises_the_error_mapped_to_the_constraint(
    session: AsyncSession,
) -> None:
    await add_duplicate_tags(session)

    with pytest.raises(TagTakenError) as caught:
        await flush(session, {"uq_scratch_tags_note_id_name": TagTakenError})

    assert isinstance(caught.value.__cause__, IntegrityError)


async def test_flush_reraises_unmapped_violations(session: AsyncSession) -> None:
    await add_duplicate_tags(session)

    with pytest.raises(IntegrityError, match="uq_scratch_tags_note_id_name"):
        await flush(session, {"ck_scratch_tags_name_not_empty": TagTakenError})


async def test_flush_reraises_violations_without_a_constraint_name(
    session: AsyncSession,
) -> None:
    session.add(ScratchNote(body=None))

    with pytest.raises(IntegrityError, match="NotNullViolation"):
        await flush(session, {"uq_scratch_tags_note_id_name": TagTakenError})
