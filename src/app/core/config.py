import re
from typing import Annotated, Literal, Self

from pydantic import NonNegativeInt, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ORIGIN_PATTERN = re.compile(r"https?://[a-z0-9.-]+(:[0-9]+)?")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True, env_ignore_empty=True, hide_input_in_errors=True
    )

    app_env: Literal["local", "test", "production"]
    secret_key: SecretStr
    database_url: SecretStr
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ()
    trusted_proxy_hops: NonNegativeInt = 0
    sentry_dsn: SecretStr | None = None
    sentry_environment: str | None = None
    git_sha: str = "unknown"

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

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            origins = (origin.strip() for origin in value.split(","))
            return tuple(origin for origin in origins if origin)
        return value

    @field_validator("cors_origins")
    @classmethod
    def check_cors_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            if origin != "*" and not ORIGIN_PATTERN.fullmatch(origin):
                raise ValueError(
                    f"{origin!r} is not an origin like https://app.example.com"
                )
        return origins

    @model_validator(mode="after")
    def check_production(self) -> Self:
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        if self.sentry_dsn is None:
            problems.append("SENTRY_DSN is required")
        if self.sentry_environment is None:
            problems.append("SENTRY_ENVIRONMENT is required")
        if self.trusted_proxy_hops == 0:
            problems.append("TRUSTED_PROXY_HOPS must be above 0")
        if not self.cors_origins or not all(
            origin.startswith("https://") for origin in self.cors_origins
        ):
            problems.append("CORS_ORIGINS must list explicit https origins, never *")
        if problems:
            raise ValueError("unsafe production settings: " + "; ".join(problems))
        return self
