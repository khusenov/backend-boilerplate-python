import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table
from sqlalchemy.orm import Mapper

import app
from app.core.db import Base
from app.core.errors import AppError


@dataclass(frozen=True)
class Model:
    mapper: Mapper[Any]
    table: Table
    module: str | None


def import_app() -> None:
    for info in pkgutil.walk_packages(app.__path__, "app."):
        importlib.import_module(info.name)


def module_of(dotted_name: str) -> str | None:
    match dotted_name.split("."):
        case ["app", "modules", name, *_]:
            return name
        case _:
            return None


def app_models() -> list[Model]:
    import_app()
    return [
        Model(mapper, mapper.local_table, module_of(mapper.class_.__module__))
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith("app.")
        and isinstance(mapper.local_table, Table)
    ]


def app_error_classes() -> list[type[AppError]]:
    import_app()
    classes: list[type[AppError]] = []
    pending: list[type[AppError]] = [AppError]
    while pending:
        error = pending.pop()
        pending.extend(error.__subclasses__())
        if error.__module__.startswith("app."):
            classes.append(error)
    return classes


def app_errors() -> list[type[AppError]]:
    return [error for error in app_error_classes() if hasattr(error, "code")]
