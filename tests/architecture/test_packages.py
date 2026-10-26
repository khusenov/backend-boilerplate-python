import pytest


def test_core_package_is_empty(pytestconfig: pytest.Config) -> None:
    init = pytestconfig.rootpath / "src" / "app" / "core" / "__init__.py"

    assert init.read_text() == ""
