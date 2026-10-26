from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.schemas import Schema


class Widget(Schema):
    name: str


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
