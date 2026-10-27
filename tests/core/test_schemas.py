from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.schemas import Email, Schema


class Widget(Schema):
    name: str


class Contact(Schema):
    email: Email


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
