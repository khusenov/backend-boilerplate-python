from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, override

from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from app.bootstrap import build_resources
from app.core.config import Settings
from app.core.http import health
from app.core.http.deps import AppState
from app.core.http.middleware import (
    AccessLogMiddleware,
    BodyLimitMiddleware,
    ClientIpMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    UnhandledErrorMiddleware,
)
from app.core.http.openapi import document_problems, operation_id
from app.core.http.problems import EXCEPTION_HANDLERS
from app.core.logging import configure_logging
from app.core.resources import Resources
from app.core.sentry import configure_sentry, flush_sentry
from app.modules.auth import router as auth


class App(FastAPI):
    @override
    def openapi(self) -> dict[str, Any]:
        return document_problems(super().openapi())


def create_app(
    settings: Settings | None = None, resources: Resources | None = None
) -> FastAPI:
    settings = settings or Settings.model_validate({})
    configure_logging(settings)
    configure_sentry(settings)
    production = settings.app_env == "production"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[AppState]:
        try:
            if resources is not None:
                yield {"settings": settings, "resources": resources}
                return
            built = build_resources(settings)
            try:
                yield {"settings": settings, "resources": built}
            finally:
                await built.close()
        finally:
            await flush_sentry()

    app = App(
        title="backend-boilerplate",
        lifespan=lifespan,
        openapi_url=None if production else "/openapi.json",
        redirect_slashes=False,
        generate_unique_id_function=operation_id,
        exception_handlers=EXCEPTION_HANDLERS,
        middleware=[
            Middleware(RequestIdMiddleware),
            Middleware(
                ClientIpMiddleware, trusted_proxy_hops=settings.trusted_proxy_hops
            ),
            Middleware(AccessLogMiddleware),
            Middleware(SecurityHeadersMiddleware, hsts=production),
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                expose_headers=["X-Request-ID"],
            ),
            Middleware(UnhandledErrorMiddleware),
            Middleware(BodyLimitMiddleware),
        ],
    )
    app.include_router(health.router)
    app.include_router(auth.router, prefix="/v1")
    return app
