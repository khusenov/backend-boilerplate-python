import pytest
from pydantic import SecretStr

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test", secret_key=SecretStr("test-secret-key"))
