from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True, env_ignore_empty=True, hide_input_in_errors=True
    )

    app_env: Literal["local", "test", "production"]
    secret_key: SecretStr

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings

    @model_validator(mode="after")
    def check_production(self) -> Self:
        if self.app_env == "production" and len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters in production")
        return self
