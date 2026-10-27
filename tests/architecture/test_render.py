import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretStr

from app.core.config import Settings

ENV_VAR = re.compile(r"^ +- key: (\w+)\n +(\w+):", re.MULTILINE)
GROUP = re.compile(
    r"^  - name: ([\w-]+)\n    envVars:\n((?:      .*\n)*)", re.MULTILINE
)
LINKED_GROUP = re.compile(r"^ +- fromGroup: ([\w-]+)$", re.MULTILINE)
SERVICE = re.compile(r"^  - type: (\w+)\n((?:    .*\n)*)", re.MULTILINE)
SERVICE_FIELD = re.compile(r"^    (\w+): (.+)$", re.MULTILINE)
COMPOSE_MIGRATE = re.compile(
    r"^  migrate:\n(?:    .*\n)*?    command: (.+)$", re.MULTILINE
)
MIGRATE_COMMAND = (
    "sh",
    "-c",
    "alembic -x ahead=skip upgrade head && alembic -x ahead=skip check",
)
SET_BY_HAND = {"SENTRY_DSN", "SMTP_PASSWORD", "SMTP_USERNAME"}


def read_blueprint(root: Path) -> str:
    return (root / "render.yaml").read_text()


def env_var_sources(root: Path) -> list[tuple[str, str]]:
    return ENV_VAR.findall(read_blueprint(root))


def services(root: Path) -> list[dict[str, str]]:
    return [
        {"type": kind} | dict(SERVICE_FIELD.findall(body))
        for kind, body in SERVICE.findall(read_blueprint(root))
    ]


def delivered_keys(body: str) -> set[str]:
    return {key for key, source in ENV_VAR.findall(body) if source != "sync"}


def service_env_vars(root: Path) -> dict[str, set[str]]:
    blueprint = read_blueprint(root)
    groups = {name: delivered_keys(body) for name, body in GROUP.findall(blueprint)}
    env_vars: dict[str, set[str]] = {}
    for _, body in SERVICE.findall(blueprint):
        linked = (groups[group] for group in LINKED_GROUP.findall(body))
        name = dict(SERVICE_FIELD.findall(body))["name"]
        env_vars[name] = delivered_keys(body).union(*linked)
    return env_vars


def test_every_staging_service_gets_every_setting_the_image_does_not(
    pytestconfig: pytest.Config,
) -> None:
    env_vars = service_env_vars(pytestconfig.rootpath)
    settings = {name.upper() for name in Settings.model_fields} - {"GIT_SHA"}

    assert env_vars
    assert {name: keys - {"PORT"} for name, keys in env_vars.items()} == dict.fromkeys(
        env_vars, settings - SET_BY_HAND
    )


def test_render_leaves_only_the_listed_settings_to_be_set_by_hand(
    pytestconfig: pytest.Config,
) -> None:
    by_hand = {
        key
        for key, source in env_var_sources(pytestconfig.rootpath)
        if source == "sync"
    }

    assert by_hand == SET_BY_HAND


def test_staging_keeps_secrets_out_of_git(pytestconfig: pytest.Config) -> None:
    secrets = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if SecretStr in (field.annotation, *get_args(field.annotation))
    }
    literals = {
        name
        for name, source in env_var_sources(pytestconfig.rootpath)
        if source == "value"
    }

    assert secrets
    assert secrets & literals == set()


def test_every_staging_service_gets_the_platform_kill_timeout(
    pytestconfig: pytest.Config,
) -> None:
    timeouts = {
        service.get("maxShutdownDelaySeconds")
        for service in services(pytestconfig.rootpath)
    }

    assert timeouts == {"45"}


def test_the_platform_health_check_is_liveness(pytestconfig: pytest.Config) -> None:
    checks = {
        service.get("healthCheckPath")
        for service in services(pytestconfig.rootpath)
        if service["type"] == "web"
    }

    assert checks == {"/health/live"}


def test_deploys_migrate_with_the_rollback_safe_command(
    pytestconfig: pytest.Config,
) -> None:
    compose = (pytestconfig.rootpath / "compose.yaml").read_text()
    [compose_command] = COMPOSE_MIGRATE.findall(compose)
    pre_deploy_commands = {
        tuple(service["preDeployCommand"].split(" ", 2))
        for service in services(pytestconfig.rootpath)
        if "preDeployCommand" in service
    }

    assert tuple(json.loads(compose_command)) == MIGRATE_COMMAND
    assert pre_deploy_commands == {MIGRATE_COMMAND}


def test_only_the_worker_migrates_before_its_deploy(
    pytestconfig: pytest.Config,
) -> None:
    migrating = [
        service["type"]
        for service in services(pytestconfig.rootpath)
        if "preDeployCommand" in service
    ]

    assert migrating == ["worker"]


def test_the_staging_worker_runs_the_worker_module(
    pytestconfig: pytest.Config,
) -> None:
    commands = [
        service.get("dockerCommand")
        for service in services(pytestconfig.rootpath)
        if service["type"] == "worker"
    ]

    assert commands == ["python -m app.worker"]
