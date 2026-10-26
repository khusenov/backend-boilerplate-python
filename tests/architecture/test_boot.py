import pytest
from fastapi.routing import iter_route_contexts
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app

DOCS_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def served_paths(settings: Settings) -> set[str | None]:
    app = create_app(settings)
    return {context.path for context in iter_route_contexts(app.routes) if context.path}


def test_app_boots(settings: Settings) -> None:
    schema = create_app(settings).openapi()

    assert schema["info"]["title"] == "backend-boilerplate"


def test_short_production_key_fails_boot(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 31)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    with pytest.raises(ValidationError) as caught:
        create_app()

    message = str(caught.value)
    assert (
        "unsafe production settings: SECRET_KEY must be at least 32 characters "
        "[type=value_error]"
    ) in message
    assert "input_value" not in message


def test_empty_secret_key_fails_boot(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


def test_missing_app_env_fails_boot(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


def test_missing_database_url_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


def test_unknown_app_env_fails_boot(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(
        ValidationError, match="Input should be 'local', 'test' or 'production'"
    ):
        create_app()


def test_docs_are_served_outside_production(settings: Settings) -> None:
    assert served_paths(settings) >= DOCS_PATHS


def test_production_serves_no_docs(production_settings: Settings) -> None:
    assert served_paths(production_settings).isdisjoint(DOCS_PATHS)
