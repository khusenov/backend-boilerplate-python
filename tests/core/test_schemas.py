from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import ValidationError

from app.core.schemas import Email, Schema, Timestamp


class Widget(Schema):
    name: str


class Contact(Schema):
    email: Email


class Event(Schema):
    at: Timestamp


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Widget.model_validate({"name": "Gear", "colour": "red"})


def test_schemas_are_frozen() -> None:
    widget = Widget(name="Gear")

    with pytest.raises(ValidationError, match="Instance is frozen"):
        widget.name = "Sprocket"


def test_schemas_read_attributes() -> None:
    row = SimpleNamespace(name="Gear", owner_id=7)

    assert Widget.model_validate(row) == Widget(name="Gear")


def test_json_schema_allows_additional_properties() -> None:
    assert "additionalProperties" not in Widget.model_json_schema()


def test_emails_are_trimmed_and_lowercased() -> None:
    assert Contact(email=" Ada@Example.COM ").email == "ada@example.com"


@pytest.mark.parametrize(
    "email",
    [
        "ada",
        "ada@example",
        "@example.com",
        "a da@example.com",
        "ada\x00@example.com",
        "x" * 243 + "@example.com",
    ],
)
def test_malformed_emails_are_rejected(email: str) -> None:
    with pytest.raises(ValidationError):
        Contact(email=email)


@pytest.mark.parametrize(
    "email",
    [
        "attacker,victim@example.com",
        "victim:attacker@evil.example;",
        "victim<attacker@evil.example>",
        "ada(comment)@example.com",
        '"ada"@example.com',
    ],
)
def test_emails_refuse_what_mail_reads_as_another_address(email: str) -> None:
    with pytest.raises(ValidationError):
        Contact(email=email)


@pytest.mark.parametrize(
    ("at", "text"),
    [
        (
            datetime(2026, 10, 14, 11, 30, 0, 123456, timezone(timedelta(hours=2))),
            "2026-10-14T09:30:00.123Z",
        ),
        (datetime(2026, 10, 14, 9, 30, tzinfo=UTC), "2026-10-14T09:30:00.000Z"),
    ],
)
def test_timestamps_are_utc_with_milliseconds(at: datetime, text: str) -> None:
    assert Event(at=at).model_dump(mode="json") == {"at": text}


def test_timestamps_must_be_aware() -> None:
    with pytest.raises(ValidationError, match="timezone_aware"):
        Event.model_validate({"at": "2026-10-14T09:30:00"})


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_timestamps_are_documented_as_date_times(
    mode: Literal["validation", "serialization"],
) -> None:
    schema = Event.model_json_schema(mode=mode)

    assert schema["properties"]["at"] == {
        "format": "date-time",
        "title": "At",
        "type": "string",
    }
