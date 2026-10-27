import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.deps import SESSION_COOKIE, CurrentPrincipal
from app.modules.users.models import User
from tests.clients import bearer, problem
from tests.factories import make_login_session, make_user
from tests.logs import json_lines

TRUSTED_ORIGIN = "https://app.example.com"
FOREIGN_ORIGIN = "https://evil.example.com"
router = APIRouter()


@router.get("/principal")
async def read_principal(principal: CurrentPrincipal) -> str:
    return str(principal.user_id)


@router.post("/principal")
async def change_principal(principal: CurrentPrincipal) -> str:
    return str(principal.user_id)


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


@pytest.fixture
async def user(session: AsyncSession) -> User:
    return await make_user(session)


@pytest.fixture
async def token(session: AsyncSession, user: User) -> str:
    return await make_login_session(session, user)


async def test_bearer_tokens_identify_the_user(
    client: AsyncClient, user: User, token: str
) -> None:
    response = await client.get("/principal", headers=bearer(token))

    assert (response.status_code, response.json()) == (200, str(user.id))


async def test_session_cookies_identify_the_user(
    client: AsyncClient, user: User, token: str
) -> None:
    client.cookies.set(SESSION_COOKIE, token)
    response = await client.get("/principal")

    assert (response.status_code, response.json()) == (200, str(user.id))


async def test_requests_without_credentials_get_401(client: AsyncClient) -> None:
    response = await client.get("/principal")

    assert problem(response) == (401, "unauthenticated")
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("credential", ["", "not-a-token"])
async def test_unknown_tokens_get_401(client: AsyncClient, credential: str) -> None:
    response = await client.get("/principal", headers=bearer(credential))

    assert problem(response) == (401, "unauthenticated")


async def test_bearer_tokens_win_over_cookies(client: AsyncClient, token: str) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-token")
    with_valid_bearer = await client.post("/principal", headers=bearer(token))
    client.cookies.set(SESSION_COOKIE, token)
    with_invalid_bearer = await client.post(
        "/principal", headers=bearer("not-a-token") | {"Origin": TRUSTED_ORIGIN}
    )

    assert with_valid_bearer.status_code == 200
    assert with_invalid_bearer.status_code == 401


async def test_other_authorization_schemes_leave_it_to_the_cookie(
    client: AsyncClient, token: str
) -> None:
    client.cookies.set(SESSION_COOKIE, token)
    response = await client.post(
        "/principal",
        headers={"Authorization": f"Basic {token}", "Origin": TRUSTED_ORIGIN},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    [FOREIGN_ORIGIN, None, "null", f"{TRUSTED_ORIGIN}/", "http://app.example.com"],
)
async def test_foreign_origin_cookie_posts_get_403(
    client: AsyncClient, token: str, origin: str | None
) -> None:
    client.cookies.set(SESSION_COOKIE, token)
    headers = {} if origin is None else {"Origin": origin}
    response = await client.post("/principal", headers=headers)

    assert problem(response) == (403, "untrusted_origin")


async def test_the_origin_is_checked_before_the_cookie(client: AsyncClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-token")
    response = await client.post("/principal", headers={"Origin": FOREIGN_ORIGIN})

    assert problem(response) == (403, "untrusted_origin")


async def test_trusted_origin_cookie_posts_pass(
    client: AsyncClient, token: str
) -> None:
    client.cookies.set(SESSION_COOKIE, token)
    response = await client.post("/principal", headers={"Origin": TRUSTED_ORIGIN})

    assert response.status_code == 200


async def test_cookie_reads_pass_from_any_origin(
    client: AsyncClient, token: str
) -> None:
    client.cookies.set(SESSION_COOKIE, token)
    response = await client.get("/principal", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 200


async def test_bearer_posts_need_no_origin(client: AsyncClient, token: str) -> None:
    response = await client.post("/principal", headers=bearer(token))

    assert response.status_code == 200


async def test_access_lines_carry_the_user_id(
    client: AsyncClient, caplog: pytest.LogCaptureFixture, user: User, token: str
) -> None:
    await client.get("/principal", headers=bearer(token))

    (access,) = [line for line in json_lines(caplog) if line["event"] == "http.request"]
    assert access["user_id"] == str(user.id)


async def test_principals_are_documented_as_bearer_or_session(
    client: AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()

    assert document["components"]["securitySchemes"] == {
        "bearer": {"type": "http", "scheme": "bearer"},
        "session": {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE},
    }
    assert document["paths"]["/principal"]["post"]["security"] == [
        {"bearer": []},
        {"session": []},
    ]
