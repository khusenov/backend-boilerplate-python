import pytest
from fastapi.routing import iter_route_contexts
from pydantic import SecretStr, ValidationError
from sentry_sdk.utils import BadDsn

from app.core.config import Settings
from app.main import create_app
from tests.sentry import FAKE_DSN

DOCS_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def served_paths(settings: Settings) -> set[str | None]:
    app = create_app(settings)
    return {context.path for context in iter_route_contexts(app.routes) if context.path}


@pytest.fixture
def production_environment(monkeypatch: pytest.MonkeyPatch, environment: None) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")


def test_app_boots(settings: Settings) -> None:
    schema = create_app(settings).openapi()

    assert schema["info"]["title"] == "backend-boilerplate"


@pytest.mark.usefixtures("production_environment")
def test_production_boots_with_smtp_tls_on_by_default() -> None:
    assert create_app().title == "backend-boilerplate"


@pytest.mark.usefixtures("production_environment")
def test_short_production_key_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "x" * 31)

    with pytest.raises(ValidationError) as caught:
        create_app()

    message = str(caught.value)
    assert (
        "unsafe production settings: SECRET_KEY must be at least 32 characters "
        "[type=value_error]"
    ) in message
    assert "input_value" not in message


@pytest.mark.usefixtures("production_environment")
def test_plaintext_smtp_fails_production_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_TLS", "off")

    with pytest.raises(ValidationError) as caught:
        create_app()

    assert (
        "unsafe production settings: SMTP_TLS must be on [type=value_error]"
    ) in str(caught.value)


def test_malformed_sentry_dsn_fails_boot(production_settings: Settings) -> None:
    update = {"sentry_dsn": SecretStr("public@sentry.invalid/1")}

    with pytest.raises(BadDsn, match="Unsupported scheme"):
        create_app(production_settings.model_copy(update=update))


@pytest.mark.usefixtures("environment")
def test_empty_secret_key_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "")

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


@pytest.mark.usefixtures("environment")
def test_missing_app_env_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV")

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


@pytest.mark.usefixtures("environment")
def test_missing_database_url_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL")

    with pytest.raises(ValidationError, match="Field required"):
        create_app()


@pytest.mark.usefixtures("environment")
def test_unknown_app_env_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(
        ValidationError, match="Input should be 'local', 'test' or 'production'"
    ):
        create_app()


def test_docs_are_served_outside_production(settings: Settings) -> None:
    assert served_paths(settings) >= DOCS_PATHS


def test_production_serves_no_docs(production_settings: Settings) -> None:
    assert served_paths(production_settings).isdisjoint(DOCS_PATHS)
