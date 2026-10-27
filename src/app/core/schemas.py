from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints
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


def drop_additional_properties(schema: JsonDict) -> None:
    schema.pop("additionalProperties", None)


class Schema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
        json_schema_extra=drop_additional_properties,
    )
