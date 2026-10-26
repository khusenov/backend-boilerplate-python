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
        ({"trusted_proxy_hops": 0}, "TRUSTED_PROXY_HOPS must be above 0"),
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
    changes = {"secret_key": "x" * 31, "trusted_proxy_hops": 0, "cors_origins": ()}
    values = production_settings.model_dump() | changes

    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)

    (error,) = caught.value.errors()
    assert error["msg"] == (
        "Value error, unsafe production settings: "
        "SECRET_KEY must be at least 32 characters; "
        f"TRUSTED_PROXY_HOPS must be above 0; {EXPLICIT_ORIGINS}"
    )


def test_unset_proxy_and_cors_settings_trust_nothing(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SECRET_KEY", "local-secret-key")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)

    settings = Settings.model_validate({})

    assert (settings.cors_origins, settings.trusted_proxy_hops) == ((), 0)


def test_trusted_proxy_hops_cannot_be_negative(settings: Settings) -> None:
    values = settings.model_dump() | {"trusted_proxy_hops": -1}

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings.model_validate(values)


def test_cors_origins_are_read_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SECRET_KEY", "local-secret-key")
    monkeypatch.setenv("DATABASE_URL", database_url)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"APP_ENV=local\nSECRET_KEY=read-from-a-file\nDATABASE_URL={database_url}\n"
    )
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

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
