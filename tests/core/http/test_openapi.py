import json
import re

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.core.http.openapi import operation_id
from app.core.schemas import Schema

PROBLEM_CONTENT = {
    "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
}
router = APIRouter()


class GadgetCreate(Schema):
    name: str


class GadgetRead(Schema):
    id: int
    name: str


@router.post("/gadgets", status_code=201)
async def create_gadget(body: GadgetCreate) -> GadgetRead:
    return GadgetRead(id=1, name=body.name)


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


async def test_operations_document_problem_json(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    operation = schema["paths"]["/gadgets"]["post"]
    assert operation["operationId"] == "test_openapi_create_gadget"
    assert sorted(operation["responses"]) == ["201", "4XX", "5XX"]
    assert operation["responses"]["4XX"]["content"] == PROBLEM_CONTENT
    assert operation["responses"]["5XX"]["content"] == PROBLEM_CONTENT


async def test_problem_schemas_replace_validation_schemas(client: AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    references = set(re.findall(r'"\$ref": "([^"]+)"', json.dumps(document)))

    assert {"GadgetCreate", "GadgetRead", "InvalidParam", "Problem"} <= schemas.keys()
    assert {"HTTPValidationError", "ValidationError"}.isdisjoint(schemas)
    assert references == {f"#/components/schemas/{name}" for name in schemas}
    assert [
        name for name, schema in schemas.items() if "additionalProperties" in schema
    ] == []


async def test_openapi_json_is_the_same_on_every_request(client: AsyncClient) -> None:
    first = (await client.get("/openapi.json")).json()

    assert (await client.get("/openapi.json")).json() == first


def test_problem_schema_names_are_reserved(app: FastAPI) -> None:
    class Problem(Schema):
        question: str

    @app.post("/problems")
    async def create_problem(body: Problem) -> None: ...

    with pytest.raises(ValueError, match="schema name Problem is reserved"):
        app.openapi()


def test_router_operations_are_named_after_their_module() -> None:
    async def list_users() -> None: ...

    list_users.__module__ = "app.modules.users.router"

    assert operation_id(APIRoute("/users", list_users)) == "users_list_users"
