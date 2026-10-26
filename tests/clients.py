from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@asynccontextmanager
async def serve(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> AsyncGenerator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(
            app=manager.app, raise_app_exceptions=raise_app_exceptions
        )
        async with AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            yield client
