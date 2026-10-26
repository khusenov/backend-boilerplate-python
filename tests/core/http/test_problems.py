from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Annotated, Literal

import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI, Header, Query
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import Field

from app.core.errors import AppError, NotFoundError
from app.core.schemas import Schema

ORIGIN = "https://app.example.com"
router = APIRouter()


class AccountNotFoundError(NotFoundError):
    code = "accounts.not_found"


class AccountInUseError(AppError):
    status = HTTPStatus.CONFLICT
    code = "accounts.in_use"


class FreePlan(Schema):
    tier: Literal["free"]


class PaidPlan(Schema):
    tier: Literal["paid"]
    seats: Annotated[int, Field(ge=1)]


class AccountCreate(Schema):
    email: Annotated[str, Field(max_length=254)]
    password: Annotated[str, Field(min_length=15, max_length=128)]
    preferences: dict[Annotated[str, Field(max_length=32)], bool]
    plan: Annotated[FreePlan | PaidPlan, Field(discriminator="tier")]


class AccountQuery(Schema):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    ids: Annotated[list[int] | None, Field(max_length=100)] = None


@router.post("/accounts", status_code=201)
async def create_account(body: AccountCreate) -> None: ...


@router.get("/accounts")
async def list_accounts(
    query: Annotated[AccountQuery, Query()],
    accept_language: Annotated[str | None, Header(max_length=35)] = None,
) -> None: ...


@router.get("/accounts/{account_id}")
async def get_account(account_id: int) -> None:
    raise AccountNotFoundError(f"Account {account_id} does not exist")


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int) -> None:
    raise AccountInUseError


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("password=hunter2")


@router.get("/stream")
async def stream() -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"first chunk"
        raise RuntimeError("stream broke")

    return StreamingResponse(chunks())


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            headers={"Origin": ORIGIN},
        ) as client:
            yield client


async def test_cross_origin_errors_are_problem_json(browser: AsyncClient) -> None:
    responses = [
        await browser.get("/missing"),
        await browser.post("/accounts", content=b"x" * (1024 * 1024 + 1)),
        await browser.post("/accounts", json={}),
        await browser.get("/boom"),
    ]

    assert [response.status_code for response in responses] == [404, 413, 422, 500]
    assert [response.json()["code"] for response in responses] == [
        "not_found",
        "content_too_large",
        "validation_failed",
        "internal_server_error",
    ]
    for response in responses:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["access-control-expose-headers"] == "X-Request-ID"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.json()["status"] == response.status_code
        assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_server_errors_reveal_nothing(browser: AsyncClient) -> None:
    response = await browser.get("/boom")

    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_server_error",
        "request_id": response.headers["x-request-id"],
    }


async def test_errors_after_the_response_starts_reach_the_server(
    client: AsyncClient,
) -> None:
    with pytest.raises(RuntimeError, match="stream broke"):
        await client.get("/stream")


async def test_app_errors_carry_their_status_and_code(client: AsyncClient) -> None:
    response = await client.get("/accounts/7")

    assert response.status_code == 404
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Account 7 does not exist",
        "code": "accounts.not_found",
        "request_id": response.headers["x-request-id"],
    }


async def test_app_errors_without_a_message_have_no_detail(
    client: AsyncClient,
) -> None:
    response = await client.delete("/accounts/7")

    assert response.status_code == 409
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "code": "accounts.in_use",
        "request_id": response.headers["x-request-id"],
    }


async def test_unknown_methods_keep_the_allow_header(client: AsyncClient) -> None:
    response = await client.post("/boom")

    assert (response.status_code, response.headers["allow"]) == (405, "GET")
    assert response.json()["code"] == "method_not_allowed"


async def test_trailing_slashes_are_not_redirected(client: AsyncClient) -> None:
    response = await client.get("/accounts/")

    assert (response.status_code, response.json()["code"]) == (404, "not_found")


async def test_body_errors_point_into_the_body(client: AsyncClient) -> None:
    body = {
        "password": "hunter2",
        "preferences": {"email/news~letter %": "maybe", "x" * 33: True},
        "plan": {"tier": "paid"},
        "role": "admin",
    }

    response = await client.post("/accounts", json=body)

    assert response.json()["errors"] == [
        {"pointer": "#/email", "detail": "Field required"},
        {
            "pointer": "#/password",
            "detail": "String should have at least 15 characters",
        },
        {
            "pointer": "#/preferences/email~1news~0letter%20%25",
            "detail": "Input should be a valid boolean, unable to interpret input",
        },
        {
            "pointer": f"#/preferences/{'x' * 33}",
            "detail": "String should have at most 32 characters",
        },
        {"pointer": "#/plan/seats", "detail": "Field required"},
        {"pointer": "#/role", "detail": "Extra inputs are not permitted"},
    ]
    assert "hunter2" not in response.text


@pytest.mark.parametrize(
    ("content", "detail"), [(b"{,}", "JSON decode error"), (b"", "Field required")]
)
async def test_unreadable_bodies_point_at_the_whole_body(
    client: AsyncClient, content: bytes, detail: str
) -> None:
    response = await client.post(
        "/accounts", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.json()["errors"] == [{"pointer": "#", "detail": detail}]


async def test_undecodable_bodies_get_400_bad_request(client: AsyncClient) -> None:
    response = await client.post(
        "/accounts",
        content=b'{"email": "\xff"}',
        headers={"Content-Type": "application/json"},
    )

    assert (response.status_code, response.headers["content-type"]) == (
        400,
        "application/problem+json",
    )
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "code": "bad_request",
        "request_id": response.headers["x-request-id"],
    }


async def test_other_inputs_name_the_parameter(client: AsyncClient) -> None:
    query = await client.get(
        "/accounts",
        params={"limit": 0, "ids": [1, "seven"], "colour": "red"},
        headers={"Accept-Language": "x" * 36},
    )
    path = await client.get("/accounts/seven")

    assert query.json()["errors"] == [
        {"parameter": "limit", "detail": "Input should be greater than or equal to 1"},
        {
            "parameter": "ids.1",
            "detail": "Input should be a valid integer, "
            "unable to parse string as an integer",
        },
        {"parameter": "colour", "detail": "Extra inputs are not permitted"},
        {
            "parameter": "accept-language",
            "detail": "String should have at most 35 characters",
        },
    ]
    assert path.json()["errors"] == [
        {
            "parameter": "account_id",
            "detail": "Input should be a valid integer, "
            "unable to parse string as an integer",
        }
    ]
