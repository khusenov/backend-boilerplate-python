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
from app.core.audit import AuditEvent
from app.core.config import Settings
from app.core.db import Base
from app.core.errors import AppError
from app.core.schemas import Schema
from app.main import create_app
from tests.architecture.inventory import (
    app_error_classes,
    app_errors,
    app_models,
    module_of,
    route_models,
)

APP_ROOT = Path(app.__path__[0])
MODULES_ROOT = Path(app.modules.__path__[0])
BANNED_CALLS = {"commit", "rollback", "exec_driver_sql", "literal_column"}
SQL_TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(\w+)", re.IGNORECASE)
SNAKE_CASE = "[a-z0-9]+(?:_[a-z0-9]+)*"
ANATOMY = {
    "router",
    "schemas",
    "service",
    "models",
    "errors",
    "tasks",
    "public",
    "queries",
    "domain",
}
CLASS_FILES = {"models.py", "schemas.py", "errors.py", "domain.py"}


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


def mentioned_names(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text())):
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name) | ast.alias(name=name):
                yield node.lineno, name
            case ast.Constant(value=str(name)):
                yield node.lineno, name


def quoted_names(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text())):
        match node:
            case ast.Constant(value=str(text)):
                yield node.lineno, text
                for table in SQL_TABLE.findall(text):
                    yield node.lineno, table


def owner_of(path: Path) -> str | None:
    return module_of(".".join(path.relative_to(APP_ROOT.parent).with_suffix("").parts))


def upserts(path: Path) -> Iterator[ast.Call]:
    for node in ast.walk(ast.parse(path.read_text())):
        match node:
            case ast.Call(func=ast.Attribute(attr="on_conflict_do_update")):
                yield node


def updated_columns(upsert: ast.Call) -> list[object]:
    return [
        key.value
        for keyword in upsert.keywords
        if keyword.arg == "set_" and isinstance(keyword.value, ast.Dict)
        for key in keyword.value.keys
        if isinstance(key, ast.Constant)
    ]


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


def test_modules_leave_audit_events_to_core() -> None:
    paths = sorted(MODULES_ROOT.rglob("*.py"))
    found = [
        f"{path.relative_to(MODULES_ROOT)}:{line}: {name}"
        for path in paths
        for line, name in mentioned_names(path)
        if name == AuditEvent.__name__ or AuditEvent.__tablename__ in name
    ]

    assert paths
    assert found == []


def test_tables_are_named_only_by_their_owner() -> None:
    owners = {model.table.name: model.module for model in app_models()}
    paths = sorted(APP_ROOT.rglob("*.py"))
    found = [
        f"{path.relative_to(APP_ROOT)}:{line}: {name}"
        for path in paths
        for line, name in quoted_names(path)
        if name in owners and owners[name] != owner_of(path)
    ]

    assert owners
    assert found == []


def test_upserts_stamp_updated_at() -> None:
    found = [
        (path, upsert)
        for path in sorted(APP_ROOT.rglob("*.py"))
        for upsert in upserts(path)
    ]
    stale = [
        f"{path.relative_to(APP_ROOT)}:{upsert.lineno}"
        for path, upsert in found
        if "updated_at" not in updated_columns(upsert)
    ]

    assert found
    assert stale == []


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


def test_public_dtos_never_serve_routes(settings: Settings) -> None:
    served = set(route_models(create_app(settings).routes))
    dtos = [
        getattr(public, name)
        for public in public_modules()
        for name in public.__all__
        if is_dto(getattr(public, name))
    ]
    doubled = [f"{dto.__module__}.{dto.__qualname__}" for dto in dtos if dto in served]

    assert served
    assert dtos
    assert doubled == []


def test_tables_carry_their_module_name() -> None:
    models = [model for model in app_models() if model.module]
    misnamed = [
        model.table.name
        for model in models
        if not re.fullmatch(f"{model.module}(?:_{SNAKE_CASE})?", model.table.name)
    ]

    assert models
    assert misnamed == []


def test_modules_hold_only_their_anatomy() -> None:
    files = [
        (name, info.name)
        for name in module_names()
        for info in pkgutil.iter_modules([str(MODULES_ROOT / name)])
    ]
    stray = [f"{name}.{file}" for name, file in files if file not in ANATOMY]

    assert files
    assert stray == []


def test_use_cases_are_plain_functions() -> None:
    paths = sorted(
        path for path in MODULES_ROOT.rglob("*.py") if path.name not in CLASS_FILES
    )
    classes = [
        f"{path.relative_to(MODULES_ROOT)}:{node.lineno}: {node.name}"
        for path in paths
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
    ]

    assert paths
    assert classes == []


def test_modules_authenticate_only_through_current_principal() -> None:
    paths = sorted(
        path
        for path in MODULES_ROOT.rglob("*.py")
        if path.relative_to(MODULES_ROOT).parts[0] != "auth"
    )
    found = [
        f"{path.relative_to(MODULES_ROOT)}:{line}: {name}"
        for path in paths
        for line, name in mentioned_names(path)
        if name == "authenticate"
    ]

    assert paths
    assert found == []


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
