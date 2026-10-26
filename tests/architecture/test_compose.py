import pytest


def test_compose_reads_nothing_from_the_host_environment(
    pytestconfig: pytest.Config,
) -> None:
    compose_files = sorted(pytestconfig.rootpath.glob("compose*.yaml"))

    assert compose_files
    assert [path.name for path in compose_files if "$" in path.read_text()] == []
