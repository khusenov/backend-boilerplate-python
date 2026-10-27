import json
import re
import typing
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from fastapi.dependencies.utils import get_flat_params
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import (
    AwareDatetime,
    BaseModel,
    FutureDatetime,
    NaiveDatetime,
    PastDatetime,
)
from pydantic_settings import BaseSettings

from app.core.config import Settings
from app.core.schemas import Schema, Timestamp
from app.main import create_app
from tests.architecture.inventory import app_subclasses

STRING_BOUNDS = {"maxLength", "enum", "const"}
BOUNDED_FORMATS = {"uuid", "date-time"}
DATETIMES = (datetime, AwareDatetime, NaiveDatetime, PastDatetime, FutureDatetime)
TIMESTAMP_PARTS = typing.get_args(Timestamp)
TYPE_KEYS = {"type", "enum", "const", "anyOf", "oneOf", "allOf"}
JSON_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def has_bare_datetime(hint: object) -> bool:
    parts = typing.get_args(hint)
    if parts[: len(TIMESTAMP_PARTS)] == TIMESTAMP_PARTS:
        return False
    return hint in DATETIMES or any(map(has_bare_datetime, parts))


def request_schemas(document: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    for path, item in document["paths"].items():
        for method, operation in item.items():
            where = f"{method.upper()} {path}"
            for parameter in operation.get("parameters", []):
                yield f"{where} {parameter['name']}", parameter["schema"]
            for media in operation.get("requestBody", {}).get("content", {}).values():
                yield f"{where} body", media["schema"]


def json_names(document: Mapping[str, Any]) -> Iterator[str]:
    for schema in document["components"]["schemas"].values():
        yield from schema.get("properties", {})
    for item in document["paths"].values():
        for operation in item.values():
            for parameter in operation.get("parameters", []):
                if parameter["in"] in {"path", "query"}:
                    yield parameter["name"]


def unbounded_inputs(
    schema: Mapping[str, Any], components: Mapping[str, Any], where: str
) -> Iterator[str]:
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/components/schemas/")
        yield from unbounded_inputs(components[name], components, where)
        return
    if not TYPE_KEYS & schema.keys() or schema.get("additionalProperties") is True:
        yield where
    kind = schema.get("type")
    bounded = STRING_BOUNDS & schema.keys() or schema.get("format") in BOUNDED_FORMATS
    if (kind == "string" and not bounded) or (
        kind == "array" and "maxItems" not in schema
    ):
        yield where
    for name, child in schema.get("properties", {}).items():
        yield from unbounded_inputs(child, components, f"{where}.{name}")
    for key in ("items", "additionalProperties", "propertyNames"):
        if isinstance(schema.get(key), dict):
            yield from unbounded_inputs(schema[key], components, f"{where}[{key}]")
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []):
            yield from unbounded_inputs(child, components, where)


def test_models_subclass_schema() -> None:
    models = app_subclasses(BaseModel)
    loose = [
        f"{model.__module__}.{model.__qualname__}"
        for model in models
        if not issubclass(model, Schema | BaseSettings)
    ]

    assert models
    assert loose == []


def test_timestamps_use_the_timestamp_type() -> None:
    schemas = app_subclasses(Schema)
    bare = [
        f"{schema.__module__}.{schema.__qualname__}.{name}"
        for schema in schemas
        for name, hint in typing.get_type_hints(schema, include_extras=True).items()
        if has_bare_datetime(hint)
    ]

    assert schemas
    assert bare == []


def test_route_parameters_use_the_timestamp_type(settings: Settings) -> None:
    params = [
        (context.path, param)
        for context in iter_route_contexts(create_app(settings).routes)
        if isinstance(context.original_route, APIRoute)
        for param in get_flat_params(context.original_route.dependant)
    ]
    bare = [
        f"{path} {param.name}"
        for path, param in params
        if has_bare_datetime(param.field_info.annotation)
    ]

    assert params
    assert bare == []


def test_schemas_keep_the_base_config() -> None:
    schemas = app_subclasses(Schema)
    loosened = [
        f"{schema.__module__}.{schema.__qualname__}"
        for schema in schemas
        if any(
            schema.model_config.get(key) != value
            for key, value in Schema.model_config.items()
        )
    ]

    assert schemas
    assert loosened == []


def test_request_strings_and_lists_are_bounded(settings: Settings) -> None:
    document = create_app(settings).openapi()
    components = document["components"]["schemas"]
    inputs = list(request_schemas(document))
    unbounded = [
        found
        for where, schema in inputs
        for found in unbounded_inputs(schema, components, where)
    ]

    assert inputs
    assert unbounded == []


def test_schemas_leave_objects_open(settings: Settings) -> None:
    document = json.dumps(create_app(settings).openapi())

    assert '"additionalProperties": false' not in document


def test_json_names_are_snake_case(settings: Settings) -> None:
    names = list(json_names(create_app(settings).openapi()))
    misnamed = [name for name in names if not JSON_NAME.fullmatch(name)]

    assert names
    assert misnamed == []
