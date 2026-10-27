import ast
import importlib
import inspect
import pkgutil
import re
import typing
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

from sqlalchemy.ext.asyncio import AsyncSession

import app.modules
from app import bootstrap
from app.core.db import Base
from app.core.errors import AppError
from app.core.schemas import Schema
from tests.architecture.inventory import (
    app_error_classes,
    app_errors,
    app_models,
    module_of,
)

MODULES_ROOT = Path(app.modules.__path__[0])
BANNED_CALLS = {"commit", "rollback", "exec_driver_sql", "literal_column"}
SNAKE_CASE = "[a-z0-9]+(?:_[a-z0-9]+)*"


def module_names() -> list[str]:
    return sorted(
        info.name for info in pkgutil.iter_modules(app.modules.__path__) if info.ispkg
    )


def public_modules() -> list[ModuleType]:
    return [
        importlib.import_module(f"app.modules.{name}.public")
        for name in module_names()
        if (MODULES_ROOT / name / "public.py").exists()
    ]


def called_names(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    originals = {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname
    }
    for node in ast.walk(tree):
        match node:
            case ast.Call(func=ast.Attribute(attr=name) | ast.Name(id=name)):
                yield node.lineno, originals.get(name, name)


def mentions_orm(annotation: object) -> bool:
    if isinstance(annotation, typing.TypeAliasType):
        return mentions_orm(annotation.__value__)
    if isinstance(annotation, type) and issubclass(annotation, Base):
        return True
    return any(mentions_orm(arg) for arg in typing.get_args(annotation))


def is_internal(value: object) -> bool:
    if isinstance(value, ModuleType):
        return value.__name__.partition(".")[0] == "app"
    if inspect.isfunction(value):
        hints = typing.get_type_hints(value)
        return "return" not in hints or any(map(mentions_orm, hints.values()))
    return mentions_orm(value)


def takes_a_session(value: object) -> bool:
    if not inspect.iscoroutinefunction(value):
        return False
    first = next(iter(inspect.signature(value).parameters.values()), None)
    return first is not None and first.annotation is AsyncSession


def is_dto(value: object) -> bool:
    return (
        isinstance(value, type)
        and issubclass(value, Schema)
        and value.model_config.get("frozen") is True
    )


def code_pattern(module: str | None) -> str:
    return rf"{module}\.{SNAKE_CASE}" if module else SNAKE_CASE


def test_bootstrap_lists_every_module() -> None:
    listed = sorted(
        models.__name__.removeprefix("app.modules.").removesuffix(".models")
        for models in bootstrap.MODELS
    )

    assert module_names()
    assert listed == module_names()


def test_modules_leave_transactions_and_raw_sql_to_core() -> None:
    paths = sorted(MODULES_ROOT.rglob("*.py"))
    found = [
        f"{path.relative_to(MODULES_ROOT)}:{line}: {name}()"
        for path in paths
        for line, name in called_names(path)
        if name in BANNED_CALLS or (name == "text" and path.name != "models.py")
    ]

    assert paths
    assert found == []


def test_public_modules_hide_internals() -> None:
    publics = public_modules()
    exposed = [
        f"{public.__name__}.{name}"
        for public in publics
        for name, value in vars(public).items()
        if not name.startswith("_") and is_internal(value)
    ]

    assert publics
    assert exposed == []


def test_public_modules_import_internals_privately() -> None:
    publics = public_modules()
    unlisted = [
        f"{public.__name__}.{name}"
        for public in publics
        for name, value in vars(public).items()
        if not name.startswith("_")
        and name not in public.__all__
        and module_of(getattr(value, "__module__", ""))
    ]

    assert publics
    assert unlisted == []


def test_public_modules_export_session_taking_functions_and_dtos() -> None:
    publics = public_modules()
    unexpected = [
        f"{public.__name__}.{name}"
        for public in publics
        for name in public.__all__
        if not (takes_a_session(getattr(public, name)) or is_dto(getattr(public, name)))
    ]

    assert publics
    assert unexpected == []


def test_tables_carry_their_module_name() -> None:
    models = [model for model in app_models() if model.module]
    misnamed = [
        model.table.name
        for model in models
        if not re.fullmatch(f"{model.module}(?:_{SNAKE_CASE})?", model.table.name)
    ]

    assert models
    assert misnamed == []


def test_error_codes_carry_their_module_name() -> None:
    errors = app_errors()
    misnamed = [
        f"{error.__module__}.{error.__qualname__}: {error.code}"
        for error in errors
        if not re.fullmatch(code_pattern(module_of(error.__module__)), error.code)
    ]

    assert errors
    assert misnamed == []


def test_only_the_core_bases_lack_a_code() -> None:
    codeless = [
        f"{error.__module__}.{error.__qualname__}"
        for error in app_error_classes()
        if error.__module__ != "app.core.errors" and not hasattr(error, "code")
    ]

    assert codeless == []


def test_module_errors_take_their_status_from_a_core_base() -> None:
    errors = [
        error for error in app_error_classes() if error.__module__ != "app.core.errors"
    ]
    stray = [
        f"{error.__module__}.{error.__qualname__}"
        for error in errors
        if AppError in error.__bases__ or "status" in vars(error)
    ]

    assert errors
    assert stray == []


def test_error_codes_are_unique() -> None:
    codes = [error.code for error in app_errors()]
    shared = sorted({code for code in codes if codes.count(code) > 1})

    assert codes
    assert shared == []
