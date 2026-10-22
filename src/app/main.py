from fastapi import FastAPI

from app.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.model_validate({})
    return FastAPI(
        title="backend-boilerplate",
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
    )
