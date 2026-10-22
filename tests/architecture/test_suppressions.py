import re
import tokenize
import tomllib
from itertools import chain
from pathlib import Path

import pytest

CHECKED_DIRECTORIES = ("src", "tests")
FORBIDDEN_DIRECTIVE = re.compile(
    r"type:\s*ignore|pyright:(?!\s*ignore\[)|(?:ruff|flake8|isort)\s*:"
)
TYPE_CHECKER_OVERRIDES = (".basedpyright", "pyrightconfig.json")
FORBIDDEN_SETTINGS = (
    "exclude_also",
    "partial_also",
    "partial_branches_always",
    "extend-ignore",
    "extend-per-file-ignores",
)


def comments(path: Path) -> list[tokenize.TokenInfo]:
    with path.open("rb") as file:
        tokens = tokenize.tokenize(file.readline)
        return [token for token in tokens if token.type == tokenize.COMMENT]


def test_rules_are_silenced_only_per_line(pytestconfig: pytest.Config) -> None:
    root = pytestconfig.rootpath
    paths = sorted(
        path for name in CHECKED_DIRECTORIES for path in (root / name).rglob("*.py")
    )
    found = [
        f"{path.relative_to(root)}:{token.start[0]}: {token.string}"
        for path in paths
        for token in comments(path)
        if FORBIDDEN_DIRECTIVE.search(token.string)
    ]

    assert paths
    assert found == []


def test_type_checker_reads_only_pyproject(pytestconfig: pytest.Config) -> None:
    root = pytestconfig.rootpath

    assert [name for name in TYPE_CHECKER_OVERRIDES if (root / name).exists()] == []


def test_gates_keep_their_settings(pytestconfig: pytest.Config) -> None:
    root = pytestconfig.rootpath
    tool = tomllib.loads((root / "pyproject.toml").read_text())["tool"]
    report, lint = tool["coverage"]["report"], tool["ruff"]["lint"]
    ignored = chain(lint["ignore"], *lint["per-file-ignores"].values())

    assert tool["coverage"]["run"]["branch"] is True
    assert report["fail_under"] >= 90
    assert (report["exclude_lines"], report["partial_branches"]) == ([], [])
    assert [key for key in FORBIDDEN_SETTINGS if key in report or key in lint] == []
    assert [rule for rule in ignored if "TID251".startswith(rule)] == []
