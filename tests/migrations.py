from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config

from tests.environment import set_test_environment

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


@contextmanager
def alembic_config(database_url: str) -> Generator[Config]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        set_test_environment(monkeypatch, database_url)
        yield Config(toml_file=PYPROJECT)
