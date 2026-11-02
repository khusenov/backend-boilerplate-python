import re

import pytest

USES_LINE = re.compile(r"^\s*(?:- )?uses: (.+)$", re.MULTILINE)
PINNED_REF = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+")


def test_actions_are_pinned_by_sha(pytestconfig: pytest.Config) -> None:
    workflows = sorted((pytestconfig.rootpath / ".github" / "workflows").glob("*.yml"))
    refs = [ref for path in workflows for ref in USES_LINE.findall(path.read_text())]

    assert refs
    assert [ref for ref in refs if not PINNED_REF.fullmatch(ref)] == []
