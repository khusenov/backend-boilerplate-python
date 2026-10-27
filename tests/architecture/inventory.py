import importlib
import pkgutil
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel
from sqlalchemy import Table
from sqlalchemy.orm import Mapper
from starlette.routing import BaseRoute

import app
from app import bootstrap
from app.core.db import Base
from app.core.errors import AppError
from app.core.jobs.queue import Message


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


def app_subclasses[T](base: type[T]) -> list[type[T]]:
    import_app()
    classes: list[type[T]] = []
    pending: list[type[T]] = [base]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("app."):
            classes.append(cls)
    return classes


def app_error_classes() -> list[type[AppError]]:
    return app_subclasses(AppError)


def app_errors() -> list[type[AppError]]:
    return [error for error in app_error_classes() if hasattr(error, "code")]


def task_names() -> list[str]:
    return sorted(
        name for blueprint in bootstrap.BLUEPRINTS for name in blueprint.tasks
    )


def app_messages() -> dict[str, type[Message]]:
    return {
        message.task_name: message
        for message in app_subclasses(Message)
        if hasattr(message, "task_name")
    }


def models_in(
    annotation: object, seen: set[type[BaseModel]] | None = None
) -> Iterator[type[BaseModel]]:
    seen = set() if seen is None else seen
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        yield annotation
        for field in annotation.model_fields.values():
            yield from models_in(field.annotation, seen)
    for part in typing.get_args(annotation):
        yield from models_in(part, seen)


def route_models(routes: list[BaseRoute]) -> Iterator[type[BaseModel]]:
    for context in iter_route_contexts(routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        yield from models_in(route.response_model)
        for param in [*route.dependant.body_params, *route.dependant.query_params]:
            yield from models_in(param.field_info.annotation)
