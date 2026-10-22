import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

CREDENTIAL_NAME = re.compile(r"(.*_)?(key|secret|password|token|dsn)|database_url")


def test_production_accepts_a_32_character_secret_key() -> None:
    settings = Settings(app_env="production", secret_key=SecretStr("x" * 32))

    assert settings.app_env == "production"


def test_production_rejects_a_31_character_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY must be at least 32"):
        Settings(app_env="production", secret_key=SecretStr("x" * 31))


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="Instance is frozen"):
        settings.app_env = "production"


def test_settings_read_only_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=local\nSECRET_KEY=read-from-a-file\n")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

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
