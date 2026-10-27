import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import Settings
from app.core.http.problems import HTTP_ERROR_CODES
from app.main import create_app
from tests.architecture.inventory import app_errors, task_names
from tests.migrations import PYPROJECT

SNAPSHOTS = Path(__file__).parent / "snapshots"


@pytest.fixture
def update_snapshots(pytestconfig: pytest.Config) -> bool:
    return bool(pytestconfig.getoption("--update-snapshots"))


def test_revisions_are_append_only(update_snapshots: bool) -> None:
    scripts = ScriptDirectory.from_config(Config(toml_file=PYPROJECT))
    parents = {
        script.revision: script.down_revision for script in scripts.walk_revisions()
    }
    snapshot = SNAPSHOTS / "revisions.json"
    published = json.loads(snapshot.read_text()) if snapshot.exists() else {}
    reverted = {
        revision: parent
        for revision, parent in published.items()
        if revision not in parents or parents[revision] != parent
    }
    assert reverted == {}, (
        "merged revisions are fixed by a new revision, never reverted"
    )
    document = json.dumps(parents, indent=2, sort_keys=True) + "\n"
    if update_snapshots:
        SNAPSHOTS.mkdir(exist_ok=True)
        snapshot.write_text(document)

    assert document == snapshot.read_text(), (
        "the revisions changed: review the diff, then run make snapshots"
    )


def test_openapi_matches_the_snapshot(
    settings: Settings, update_snapshots: bool
) -> None:
    schema = create_app(settings).openapi()
    document = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    snapshot = SNAPSHOTS / "openapi.json"
    if update_snapshots:
        SNAPSHOTS.mkdir(exist_ok=True)
        snapshot.write_text(document)

    assert document == snapshot.read_text(), (
        "the OpenAPI schema changed: review the diff, then run make snapshots"
    )


def test_error_codes_are_append_only(update_snapshots: bool) -> None:
    codes = {code: status.value for status, code in HTTP_ERROR_CODES.items()}
    codes |= {error.code: error.status.value for error in app_errors()}
    snapshot = SNAPSHOTS / "error_codes.json"
    published = json.loads(snapshot.read_text()) if snapshot.exists() else {}
    changed = {
        code: status for code, status in published.items() if codes.get(code) != status
    }
    assert changed == {}, "error codes are append-only: restore these codes"
    document = json.dumps(codes, indent=2, sort_keys=True) + "\n"
    if update_snapshots:
        snapshot.write_text(document)

    assert document == snapshot.read_text(), (
        "the error codes changed: review the diff, then run make snapshots"
    )


def test_task_names_match_the_snapshot(update_snapshots: bool) -> None:
    document = json.dumps(task_names(), indent=2) + "\n"
    snapshot = SNAPSHOTS / "task_names.json"
    if update_snapshots:
        snapshot.write_text(document)

    assert document == snapshot.read_text(), (
        "the task names changed: review the diff, then run make snapshots"
    )
