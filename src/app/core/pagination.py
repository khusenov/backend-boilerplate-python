import base64
import uuid
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, Field

from app.core.schemas import Schema

CURSOR_VERSION = b"\x01"


def encode_cursor(last_id: uuid.UUID) -> str:
    raw = CURSOR_VERSION + last_id.bytes
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except ValueError as error:
        raise ValueError("Invalid cursor") from error
    version, last_id = raw[:1], raw[1:]
    if version != CURSOR_VERSION or len(last_id) != 16:
        raise ValueError("Invalid cursor")
    return uuid.UUID(bytes=last_id)


def check_cursor(cursor: str) -> str:
    decode_cursor(cursor)
    return cursor


Cursor = Annotated[str, Field(max_length=64), AfterValidator(check_cursor)]


class PageQuery(Schema):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None
