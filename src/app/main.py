from typing import Any, override

from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings
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


class App(FastAPI):
    @override
    def openapi(self) -> dict[str, Any]:
        return document_problems(super().openapi())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.model_validate({})
    configure_logging(settings)
    production = settings.app_env == "production"
    return App(
        title="backend-boilerplate",
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
