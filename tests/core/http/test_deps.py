from http import HTTPStatus

import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.http.deps import DbSession
from tests.clients import serve
from tests.scratch import ScratchNote

pytestmark = pytest.mark.usefixtures("scratch_tables")
router = APIRouter()


class NoteRejectedError(AppError):
    status = HTTPStatus.CONFLICT
    code = "notes.rejected"


def refuse_commit(_session: Session) -> None:
    raise RuntimeError("commit refused")


@router.post("/notes", status_code=201)
async def create_note(session: DbSession) -> None:
    session.add(ScratchNote(body="saved"))


@router.post("/rejected-notes", status_code=201)
async def reject_note(session: DbSession) -> None:
    session.add(ScratchNote(body="rejected"))
    await session.flush()
    raise NoteRejectedError


@router.post("/uncommitted-notes", status_code=201)
async def create_uncommitted_note(session: DbSession) -> None:
    session.add(ScratchNote(body="uncommitted"))
    await session.flush()
    event.listen(session.sync_session, "before_commit", refuse_commit)


@router.get("/notes")
async def create_note_on_read(session: DbSession) -> None:
    session.add(ScratchNote(body="read"))
    await session.flush()


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


async def note_bodies(connection: AsyncConnection) -> list[str]:
    return list(await connection.scalars(select(ScratchNote.body)))


async def test_requests_commit_their_writes(
    client: AsyncClient, connection: AsyncConnection
) -> None:
    response = await client.post("/notes")

    assert response.status_code == 201
    assert await note_bodies(connection) == ["saved"]


async def test_a_write_followed_by_a_raise_leaves_no_row(
    client: AsyncClient, connection: AsyncConnection
) -> None:
    response = await client.post("/rejected-notes")

    assert (response.status_code, response.json()["code"]) == (409, "notes.rejected")
    assert await note_bodies(connection) == []


async def test_a_failed_commit_fails_the_request(
    app: FastAPI, connection: AsyncConnection
) -> None:
    async with serve(app, raise_app_exceptions=False) as client:
        response = await client.post("/uncommitted-notes")

    assert response.status_code == 500
    assert await note_bodies(connection) == []


async def test_gets_never_change_state(
    app: FastAPI, connection: AsyncConnection
) -> None:
    async with serve(app, raise_app_exceptions=False) as client:
        response = await client.get("/notes")

    assert response.status_code == 500
    assert await note_bodies(connection) == []
