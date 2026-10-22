from collections import Counter
from pathlib import Path

import pytest

from app.core.config import Settings


def read_env_example(root: Path) -> list[tuple[str, str]]:
    lines = (root / ".env.example").read_text().splitlines()
    return [(name, value) for name, _, value in (line.partition("=") for line in lines)]


def test_env_example_lists_every_setting_once(pytestconfig: pytest.Config) -> None:
    names = Counter(name for name, _ in read_env_example(pytestconfig.rootpath))

    assert names == {name.upper(): 1 for name in Settings.model_fields}


def test_env_example_builds_local_settings(
    pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in read_env_example(pytestconfig.rootpath):
        monkeypatch.setenv(name, value)

    assert Settings.model_validate({}).app_env == "local"
