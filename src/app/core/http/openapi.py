from typing import Any

from fastapi.routing import APIRoute
from pydantic.json_schema import models_json_schema

from app.core.http.problems import PROBLEM_JSON, Problem


def operation_id(route: APIRoute) -> str:
    package, _, module = route.endpoint.__module__.rpartition(".")
    name = package.rpartition(".")[2] if module == "router" else module
    return f"{name}_{route.endpoint.__name__}"


def document_problems(schema: dict[str, Any]) -> dict[str, Any]:
    _, definitions = models_json_schema(
        [(Problem, "serialization")], ref_template="#/components/schemas/{model}"
    )
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    for name, definition in definitions["$defs"].items():
        if schemas.setdefault(name, definition) != definition:
            raise ValueError(f"the schema name {name} is reserved for problem JSON")
    content = {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}}
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            responses = operation["responses"]
            responses.pop("422", None)
            responses["4XX"] = {"description": "Client error", "content": content}
            responses["5XX"] = {"description": "Server error", "content": content}
    return schema
