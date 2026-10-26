from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


@contextmanager
def alembic_config(database_url: str) -> Generator[Config]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("SECRET_KEY", "test-secret-key")
        monkeypatch.setenv("DATABASE_URL", database_url)
        yield Config(toml_file=PYPROJECT)
