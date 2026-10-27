import base64
import uuid

import pytest
from pydantic import ValidationError

from app.core.pagination import PageQuery, decode_cursor, encode_cursor

LAST_ID = uuid.UUID("0199e5a4-3c2b-7d1e-8f00-1234567890ab")
CURSOR = encode_cursor(LAST_ID)


def test_cursors_decode_to_the_id_they_encode() -> None:
    assert decode_cursor(CURSOR) == LAST_ID


def test_cursors_are_short_url_safe_strings() -> None:
    assert CURSOR == "AQGZ5aQ8K30ejwASNFZ4kKs"


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "abc",
        "!!!!",
        "é",
        CURSOR[:-1],
        CURSOR + "A",
        base64.urlsafe_b64encode(b"\x02" + LAST_ID.bytes).decode(),
    ],
)
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValidationError, match="Invalid cursor"):
        PageQuery(cursor=cursor)


def test_lists_start_at_the_top_with_twenty_items() -> None:
    assert PageQuery() == PageQuery(limit=20, cursor=None)


@pytest.mark.parametrize("limit", [1, 100])
def test_limits_from_one_to_a_hundred_are_accepted(limit: int) -> None:
    assert PageQuery(limit=limit).limit == limit


@pytest.mark.parametrize("fields", [{"limit": 0}, {"limit": 101}, {"cursor": "A" * 65}])
def test_out_of_range_queries_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PageQuery.model_validate(fields)
