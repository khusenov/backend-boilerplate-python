import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

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
