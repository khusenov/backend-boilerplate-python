import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

CREDENTIAL_NAME = re.compile(r"(.*_)?(key|secret|password|token|dsn)|database_url")
EXPLICIT_ORIGINS = "CORS_ORIGINS must list explicit https origins, never *"


def test_production_accepts_safe_settings(production_settings: Settings) -> None:
    values = production_settings.model_dump()

    assert Settings.model_validate(values) == production_settings


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"secret_key": "x" * 31}, "SECRET_KEY must be at least 32 characters"),
        ({"sentry_dsn": None}, "SENTRY_DSN is required"),
        ({"sentry_environment": None}, "SENTRY_ENVIRONMENT is required"),
        ({"trusted_proxy_hops": 0}, "TRUSTED_PROXY_HOPS must be above 0"),
        ({"smtp_tls": False}, "SMTP_TLS must be on"),
        ({"cors_origins": ()}, EXPLICIT_ORIGINS),
        ({"cors_origins": ("*",)}, EXPLICIT_ORIGINS),
        ({"cors_origins": ("https://app.example.com", "*")}, EXPLICIT_ORIGINS),
        ({"cors_origins": ("http://app.example.com",)}, EXPLICIT_ORIGINS),
    ],
)
def test_production_rejects_unsafe_settings(
    production_settings: Settings, changes: dict[str, object], problem: str
) -> None:
    values = production_settings.model_dump() | changes

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["msg"] == f"Value error, unsafe production settings: {problem}"


def test_production_lists_every_problem_at_once(
    production_settings: Settings,
) -> None:
    changes = {
        "secret_key": "x" * 31,
        "sentry_dsn": None,
        "sentry_environment": None,
        "trusted_proxy_hops": 0,
        "smtp_tls": False,
        "cors_origins": (),
    }
    values = production_settings.model_dump() | changes

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["msg"] == (
        "Value error, unsafe production settings: "
        "SECRET_KEY must be at least 32 characters; "
        "SENTRY_DSN is required; SENTRY_ENVIRONMENT is required; "
        "TRUSTED_PROXY_HOPS must be above 0; SMTP_TLS must be on; "
        f"{EXPLICIT_ORIGINS}"
    )


@pytest.mark.usefixtures("environment")
def test_unset_proxy_and_cors_settings_trust_nothing() -> None:
    settings = Settings.model_validate({})

    assert (settings.cors_origins, settings.trusted_proxy_hops) == ((), 0)


@pytest.mark.usefixtures("environment")
def test_unset_sentry_settings_report_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "")

    settings = Settings.model_validate({})

    assert (settings.sentry_dsn, settings.sentry_environment, settings.git_sha) == (
        None,
        None,
        "unknown",
    )


@pytest.mark.usefixtures("environment")
def test_unset_smtp_settings_mean_starttls_on_the_submission_port() -> None:
    settings = Settings.model_validate({})

    assert (
        settings.smtp_port,
        settings.smtp_tls,
        settings.smtp_username,
        settings.smtp_password,
    ) == (587, True, None, None)


@pytest.mark.parametrize(
    "credentials",
    [{"smtp_username": "staging"}, {"smtp_password": "smtp-password"}],
)
def test_smtp_credentials_come_in_pairs(
    settings: Settings, credentials: dict[str, object]
) -> None:
    values = settings.model_dump() | credentials

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["msg"] == (
        "Value error, SMTP_USERNAME and SMTP_PASSWORD must be set together"
    )


def test_smtp_credentials_may_be_set_together(settings: Settings) -> None:
    credentials = {"smtp_username": "staging", "smtp_password": "smtp-password"}
    values = settings.model_dump() | credentials

    validated = Settings.model_validate(values)

    assert (validated.smtp_username, validated.smtp_password) == (
        SecretStr("staging"),
        SecretStr("smtp-password"),
    )


@pytest.mark.parametrize("port", [0, 65536])
def test_smtp_port_must_be_a_tcp_port(settings: Settings, port: int) -> None:
    values = settings.model_dump() | {"smtp_port": port}

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["loc"] == ("smtp_port",)


@pytest.mark.parametrize("address", ["Backend <noreply@example.com>", "noreply"])
def test_email_from_must_be_a_plain_address(settings: Settings, address: str) -> None:
    values = settings.model_dump() | {"email_from": address}

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["loc"] == ("email_from",)


def test_trusted_proxy_hops_cannot_be_negative(settings: Settings) -> None:
    values = settings.model_dump() | {"trusted_proxy_hops": -1}

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings.model_validate(values)


@pytest.mark.usefixtures("environment")
def test_cors_origins_are_read_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS", " https://app.example.com,,http://localhost:3000, "
    )
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")

    settings = Settings.model_validate({})

    assert settings.cors_origins == ("https://app.example.com", "http://localhost:3000")


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com/",
        "https://app.example.com/v1",
        "app.example.com",
        "ftp://app.example.com",
        "https://*.example.com",
        "https://App.example.com",
        "https://user@app.example.com",
    ],
)
def test_cors_origins_must_be_bare_origins(settings: Settings, origin: str) -> None:
    values = settings.model_dump() | {"cors_origins": (origin,)}

    with pytest.raises(ValidationError, match="is not an origin like https://"):
        Settings.model_validate(values)


def test_any_origin_is_allowed_outside_production(settings: Settings) -> None:
    values = settings.model_dump() | {"cors_origins": ("*",)}

    assert Settings.model_validate(values).cors_origins == ("*",)


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="Instance is frozen"):
        settings.app_env = "production"


def test_settings_read_only_environment_variables(
    tmp_path: Path, database_url: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"APP_ENV=local\nSECRET_KEY=read-from-a-file\nDATABASE_URL={database_url}\n"
    )

    with pytest.raises(ValidationError, match="Field required"):
        Settings.model_validate({"_env_file": env_file})


def test_credential_settings_are_secret_strings() -> None:
    credentials = {
        name: (field.annotation, *get_args(field.annotation))
        for name, field in Settings.model_fields.items()
        if CREDENTIAL_NAME.fullmatch(name)
    }

    assert credentials
    assert [name for name, types in credentials.items() if SecretStr not in types] == []
