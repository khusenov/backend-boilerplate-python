import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Role
from app.modules.users.models import User, UserStatus
from tests.audit import audit_rows
from tests.clients import bearer, problem
from tests.factories import make_login_session, make_user

UNAUTHENTICATED = (401, "unauthenticated")
DENIED = (403, "permission_denied")
NOT_FOUND = (404, "users.not_found")
OK = (200, None)

MATRIX = [
    ("GET", "/v1/users", UNAUTHENTICATED, OK, OK),
    ("POST", "/v1/users", UNAUTHENTICATED, DENIED, (201, None)),
    ("GET", "/v1/users/me", UNAUTHENTICATED, OK, OK),
    ("PATCH", "/v1/users/me", UNAUTHENTICATED, OK, OK),
    ("GET", "/v1/users/{own}", UNAUTHENTICATED, OK, OK),
    ("GET", "/v1/users/{other}", UNAUTHENTICATED, NOT_FOUND, OK),
    ("GET", "/v1/users/{missing}", UNAUTHENTICATED, NOT_FOUND, NOT_FOUND),
    ("PATCH", "/v1/users/{own}", UNAUTHENTICATED, DENIED, OK),
    ("PATCH", "/v1/users/{other}", UNAUTHENTICATED, DENIED, OK),
    ("PATCH", "/v1/users/{missing}", UNAUTHENTICATED, DENIED, NOT_FOUND),
    ("DELETE", "/v1/users/{own}", UNAUTHENTICATED, DENIED, (204, None)),
    ("DELETE", "/v1/users/{other}", UNAUTHENTICATED, DENIED, (204, None)),
    ("DELETE", "/v1/users/{missing}", UNAUTHENTICATED, DENIED, NOT_FOUND),
]
CALLERS = [None, Role.MEMBER, Role.ADMIN]
BODIES = {
    "POST": {"email": "new@example.com", "name": "New User"},
    "PATCH": {"name": "New Name"},
}


async def headers_for(session: AsyncSession, user: User) -> dict[str, str]:
    return bearer(await make_login_session(session, user))


def ids(response: Response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize(
    ("role", "method", "path", "expected"),
    [
        pytest.param(
            role, method, path, answer, id=f"{role or 'anonymous'} {method} {path}"
        )
        for method, path, *answers in MATRIX
        for role, answer in zip(CALLERS, answers, strict=True)
    ],
)
async def test_each_role_gets_its_answer(
    client: AsyncClient,
    session: AsyncSession,
    role: Role | None,
    method: str,
    path: str,
    expected: tuple[int, str | None],
) -> None:
    caller = await make_user(session, role=role or Role.MEMBER)
    other = await make_user(session)
    headers = {} if role is None else await headers_for(session, caller)
    url = path.format(own=caller.id, other=other.id, missing=uuid.uuid7())

    response = await client.request(
        method, url, headers=headers, json=BODIES.get(method)
    )

    assert problem(response) == expected


async def test_lists_hold_only_the_users_the_caller_may_see(
    client: AsyncClient, session: AsyncSession
) -> None:
    member = await make_user(session)
    admin = await make_user(session, role=Role.ADMIN)
    await make_user(session, status=UserStatus.DEACTIVATED)

    seen_by_member = await client.get(
        "/v1/users", headers=await headers_for(session, member)
    )
    seen_by_admin = await client.get(
        "/v1/users", headers=await headers_for(session, admin)
    )

    assert ids(seen_by_member) == [str(member.id)]
    assert ids(seen_by_admin) == [str(admin.id), str(member.id)]


async def test_lists_page_with_a_cursor(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    older, newer = [await make_user(session) for _ in range(2)]
    headers = await headers_for(session, admin)

    first = await client.get("/v1/users", params={"limit": 2}, headers=headers)
    cursor = first.json()["next_cursor"]
    second = await client.get(
        "/v1/users", params={"limit": 2, "cursor": cursor}, headers=headers
    )

    assert ids(first) == [str(newer.id), str(older.id)]
    assert (ids(second), second.json()["next_cursor"]) == ([str(admin.id)], None)


async def test_malformed_cursors_get_422(
    client: AsyncClient, session: AsyncSession
) -> None:
    member = await make_user(session)
    response = await client.get(
        "/v1/users",
        params={"cursor": "not-a-cursor"},
        headers=await headers_for(session, member),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [
        {"parameter": "cursor", "detail": "Value error, Invalid cursor"}
    ]


async def test_users_read_with_their_role_and_timestamps(
    client: AsyncClient, session: AsyncSession
) -> None:
    member = await make_user(session, email="ada@example.com", name="Ada Lovelace")
    response = await client.get(
        "/v1/users/me", headers=await headers_for(session, member)
    )

    body = response.json()
    assert sorted(body) == ["created_at", "email", "id", "name", "role", "updated_at"]
    assert (body["id"], body["email"], body["name"], body["role"]) == (
        str(member.id),
        "ada@example.com",
        "Ada Lovelace",
        "member",
    )
    assert body["created_at"].endswith("Z")


async def test_admins_create_members(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    response = await client.post(
        "/v1/users",
        json={"email": " Ada@Example.com ", "name": "Ada Lovelace"},
        headers=await headers_for(session, admin),
    )

    created = await session.get(User, uuid.UUID(response.json()["id"]))
    assert response.status_code == 201
    assert created is not None
    assert (created.email, created.role) == ("ada@example.com", Role.MEMBER)


async def test_a_taken_email_gets_409(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    response = await client.post(
        "/v1/users",
        json={"email": admin.email, "name": "Ada Lovelace"},
        headers=await headers_for(session, admin),
    )

    assert problem(response) == (409, "users.email_taken")


async def test_a_members_self_patch_with_role_gets_422(
    client: AsyncClient, session: AsyncSession
) -> None:
    member = await make_user(session)
    response = await client.patch(
        "/v1/users/me",
        json={"role": "admin"},
        headers=await headers_for(session, member),
    )

    await session.refresh(member)
    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [
        {"pointer": "#/role", "detail": "Extra inputs are not permitted"}
    ]
    assert member.role == Role.MEMBER


@pytest.mark.parametrize("path", ["/v1/users/me", "/v1/users/{own}"])
async def test_patches_never_change_the_email(
    client: AsyncClient, session: AsyncSession, path: str
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    response = await client.patch(
        path.format(own=admin.id),
        json={"email": "grace@example.com"},
        headers=await headers_for(session, admin),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"][0]["pointer"] == "#/email"


@pytest.mark.parametrize(
    ("path", "body"),
    [("/v1/users/me", {"name": None}), ("/v1/users/{own}", {"role": None})],
)
async def test_null_clears_no_field(
    client: AsyncClient, session: AsyncSession, path: str, body: dict[str, None]
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    response = await client.patch(
        path.format(own=admin.id), json=body, headers=await headers_for(session, admin)
    )

    assert problem(response) == (422, "validation_failed")


async def test_patches_change_only_the_fields_sent(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    member = await make_user(session, name="Ada Lovelace")
    member_headers = await headers_for(session, member)
    renamed = await client.patch(
        "/v1/users/me", json={"name": " Ada King "}, headers=member_headers
    )
    untouched = await client.patch("/v1/users/me", json={}, headers=member_headers)
    promoted = await client.patch(
        f"/v1/users/{member.id}",
        json={"role": "admin"},
        headers=await headers_for(session, admin),
    )

    assert (renamed.json()["name"], renamed.json()["role"]) == ("Ada King", "member")
    assert untouched.json() == renamed.json()
    assert (promoted.json()["name"], promoted.json()["role"]) == ("Ada King", "admin")


async def test_a_role_change_writes_one_audit_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    member = await make_user(session)
    traced = {"X-Forwarded-For": "203.0.113.7"}
    headers = await headers_for(session, admin) | traced
    response = await client.patch(
        f"/v1/users/{member.id}", json={"role": "admin"}, headers=headers
    )

    assert response.status_code == 200
    assert await audit_rows(session) == [
        (
            admin.id,
            "users.role_changed",
            member.id,
            "203.0.113.7",
            response.headers["x-request-id"],
            {"role": "admin"},
        )
    ]


async def test_role_changes_apply_on_the_next_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    member = await make_user(session)
    headers = await headers_for(session, member)
    before = await client.get(f"/v1/users/{admin.id}", headers=headers)
    await client.patch(
        f"/v1/users/{member.id}",
        json={"role": "admin"},
        headers=await headers_for(session, admin),
    )
    after = await client.get(f"/v1/users/{admin.id}", headers=headers)

    assert problem(before) == NOT_FOUND
    assert after.status_code == 200


async def test_deactivation_hides_the_user_and_ends_their_access(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await make_user(session, role=Role.ADMIN)
    member = await make_user(session)
    member_headers = await headers_for(session, member)
    admin_headers = await headers_for(session, admin)
    traced = {"X-Forwarded-For": "203.0.113.7"}
    response = await client.delete(
        f"/v1/users/{member.id}", headers=admin_headers | traced
    )
    lookup = await client.get(f"/v1/users/{member.id}", headers=admin_headers)
    own_view = await client.get("/v1/users/me", headers=member_headers)

    assert (response.status_code, response.content) == (204, b"")
    assert "content-type" not in response.headers
    assert problem(lookup) == NOT_FOUND
    assert problem(own_view) == UNAUTHENTICATED
    assert await audit_rows(session) == [
        (
            admin.id,
            "users.deactivated",
            member.id,
            "203.0.113.7",
            response.headers["x-request-id"],
            {"status": "deactivated"},
        )
    ]
