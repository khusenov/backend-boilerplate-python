from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import ASGIApp

from app.core.http.problems import PROBLEM_JSON


def connect(
    app: ASGIApp, *, address: str = "127.0.0.1", raise_app_exceptions: bool = True
) -> AsyncClient:
    transport = ASGITransport(
        app=app, raise_app_exceptions=raise_app_exceptions, client=(address, 123)
    )
    return AsyncClient(transport=transport, base_url="https://testserver")


@asynccontextmanager
async def serve(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> AsyncGenerator[AsyncClient]:
    async with (
        LifespanManager(app) as manager,
        connect(manager.app, raise_app_exceptions=raise_app_exceptions) as client,
    ):
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def problem(response: Response) -> tuple[int, str | None]:
    if response.headers.get("content-type") != PROBLEM_JSON:
        return response.status_code, None
    return response.status_code, response.json()["code"]
