import json
from pathlib import Path

import pytest


def runtime_stage_arguments(root: Path, instruction: str) -> list[str]:
    lines = (root / "Dockerfile").read_text().splitlines()
    start = max(index for index, line in enumerate(lines) if line.startswith("FROM "))
    prefix = f"{instruction} "
    return [
        line.removeprefix(prefix) for line in lines[start:] if line.startswith(prefix)
    ]


def makefile_uvicorn_command(root: Path) -> list[str]:
    makefile = (root / "Makefile").read_text().replace("\\\n", " ")
    [line] = [line for line in makefile.splitlines() if " uvicorn " in line]
    words = line.split()
    return words[words.index("uvicorn") :]


def api_command(options: str) -> list[str]:
    return (
        f"uvicorn app.main:create_app --factory {options} --no-proxy-headers"
        " --no-access-log --timeout-keep-alive 120 --timeout-graceful-shutdown 20"
    ).split()


def test_the_image_runs_as_a_non_root_user(pytestconfig: pytest.Config) -> None:
    users = runtime_stage_arguments(pytestconfig.rootpath, "USER")

    assert users
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_the_image_runs_the_api_command_in_exec_form(
    pytestconfig: pytest.Config,
) -> None:
    [command] = runtime_stage_arguments(pytestconfig.rootpath, "CMD")

    assert json.loads(command) == api_command("--host 0.0.0.0 --port 8000")


def test_the_image_tells_the_app_its_commit(pytestconfig: pytest.Config) -> None:
    environment = runtime_stage_arguments(pytestconfig.rootpath, "ENV")

    assert "GIT_SHA=$GIT_SHA" in environment


def test_make_runs_the_api_command_with_reload(pytestconfig: pytest.Config) -> None:
    command = makefile_uvicorn_command(pytestconfig.rootpath)

    assert command == api_command("--reload --reload-dir src")
