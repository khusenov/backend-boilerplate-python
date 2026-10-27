from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
)
from pydantic.config import JsonDict

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=254,
        pattern=r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$",
    ),
]


def format_timestamp(value: datetime) -> str:
    utc = value.astimezone(UTC).isoformat(timespec="milliseconds")
    return utc.replace("+00:00", "Z")


Timestamp = Annotated[
    AwareDatetime,
    PlainSerializer(format_timestamp, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


def drop_additional_properties(schema: JsonDict) -> None:
    schema.pop("additionalProperties", None)


class Schema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
        json_schema_extra=drop_additional_properties,
    )
