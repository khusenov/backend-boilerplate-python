import re

import pytest

USES_LINE = re.compile(r"^\s*(?:- )?uses: (.+)$", re.MULTILINE)
PINNED_REF = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+")
WAITED_DEPLOY = re.compile(r'^ +render deploys create "\$(\w+)" .*--wait', re.MULTILINE)
MAIN_TAG = re.compile(r'^ +docker (?:tag|push) .*:main"$', re.MULTILINE)


def test_actions_are_pinned_by_sha(pytestconfig: pytest.Config) -> None:
    workflows = sorted((pytestconfig.rootpath / ".github" / "workflows").glob("*.yml"))
    refs = [ref for path in workflows for ref in USES_LINE.findall(path.read_text())]

    assert refs
    assert [ref for ref in refs if not PINNED_REF.fullmatch(ref)] == []


def test_staging_deploys_the_worker_before_the_api(pytestconfig: pytest.Config) -> None:
    ci = (pytestconfig.rootpath / ".github" / "workflows" / "ci.yml").read_text()

    assert WAITED_DEPLOY.findall(ci) == [
        "RENDER_WORKER_SERVICE_ID",
        "RENDER_SERVICE_ID",
    ]


def test_staging_moves_main_after_the_api_deploy(pytestconfig: pytest.Config) -> None:
    ci = (pytestconfig.rootpath / ".github" / "workflows" / "ci.yml").read_text()
    live = max(deploy.end() for deploy in WAITED_DEPLOY.finditer(ci))

    assert MAIN_TAG.findall(ci[:live]) == []
    assert MAIN_TAG.findall(ci[live:]) != []
