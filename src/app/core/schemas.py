from pydantic import BaseModel, ConfigDict
from pydantic.config import JsonDict


def drop_additional_properties(schema: JsonDict) -> None:
    schema.pop("additionalProperties", None)


class Schema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
        json_schema_extra=drop_additional_properties,
    )
