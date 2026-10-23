from fastapi import FastAPI
from fastapi.middleware import Middleware

from app.core.config import Settings
from app.core.http.middleware import AccessLogMiddleware, RequestIdMiddleware
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.model_validate({})
    configure_logging(settings)
    return FastAPI(
        title="backend-boilerplate",
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
        middleware=[Middleware(RequestIdMiddleware), Middleware(AccessLogMiddleware)],
    )
