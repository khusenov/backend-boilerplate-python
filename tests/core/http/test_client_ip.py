import pytest
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.http.client_ip import client_address, forwarded_client_ip

router = APIRouter()


@router.get("/client-ip")
async def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    ("forwarded_for", "trusted_hops", "expected"),
    [
        ("", 1, None),
        ("203.0.113.7", 0, None),
        ("203.0.113.7", 1, "203.0.113.7"),
        ("6.6.6.6, 203.0.113.7", 1, "203.0.113.7"),
        ("6.6.6.6, 203.0.113.7, 198.51.100.1", 2, "203.0.113.7"),
        ("203.0.113.7", 2, None),
        ("6.6.6.6, not-an-ip", 1, None),
        ("6.6.6.6, ", 1, None),
        ("2001:DB8::0:1", 1, "2001:db8::1"),
        ("[2001:db8::1]:4711", 1, "2001:db8::1"),
        ("203.0.113.7:51234", 1, "203.0.113.7"),
        ("::ffff:203.0.113.7", 1, "203.0.113.7"),
    ],
)
def test_forwarded_client_ip(
    forwarded_for: str, trusted_hops: int, expected: str | None
) -> None:
    assert forwarded_client_ip(forwarded_for, trusted_hops) == expected


@pytest.mark.parametrize(
    ("forwarded_for", "peer", "expected"),
    [
        ("6.6.6.6, 203.0.113.7", ("10.0.0.2", 51234), ("203.0.113.7", 0)),
        ("", ("::ffff:203.0.113.7", 51234), ("203.0.113.7", 51234)),
        ("", ("testclient", 50000), ("testclient", 50000)),
        ("", None, None),
    ],
)
def test_client_address(
    forwarded_for: str,
    peer: tuple[str, int] | None,
    expected: tuple[str, int] | None,
) -> None:
    assert client_address(forwarded_for, 1, peer) == expected


async def test_client_ip_is_the_entry_the_proxy_added(client: AsyncClient) -> None:
    single = await client.get(
        "/client-ip", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}
    )
    repeated = await client.get(
        "/client-ip",
        headers=[("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "203.0.113.7")],
    )

    assert [single.json(), repeated.json()] == ["203.0.113.7", "203.0.113.7"]


async def test_client_ip_falls_back_to_the_peer(client: AsyncClient) -> None:
    missing = await client.get("/client-ip")
    garbage = await client.get("/client-ip", headers={"X-Forwarded-For": "unknown"})

    assert [missing.json(), garbage.json()] == ["127.0.0.1", "127.0.0.1"]


async def test_client_ip_unwraps_an_ipv4_mapped_peer(app: FastAPI) -> None:
    transport = ASGITransport(app=app, client=("::ffff:203.0.113.7", 51234))
    async with AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as client:
        response = await client.get("/client-ip")

    assert response.json() == "203.0.113.7"
