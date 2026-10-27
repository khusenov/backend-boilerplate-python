import uuid
from typing import Any

from sqlalchemy import (
    Column,
    ColumnDefault,
    DateTime,
    DefaultClause,
    Enum,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    Uuid,
)

from app.core.db import Base
from tests.architecture.inventory import app_models

ON_DELETE_RULES = {"RESTRICT", "CASCADE"}


def is_uuid7_id(column: Column[Any]) -> bool:
    default, server_default = column.default, column.server_default
    return (
        column.name == "id"
        and isinstance(column.type, Uuid)
        and isinstance(default, ColumnDefault)
        and getattr(default.arg, "__wrapped__", default.arg) is uuid.uuid7
        and isinstance(server_default, DefaultClause)
        and str(server_default.arg) == "uuidv7()"
    )


def has_uuid7_key(table: Table) -> bool:
    match list(table.primary_key):
        case [column]:
            return is_uuid7_id(column)
        case _:
            return False


def is_indexed(constraint: ForeignKeyConstraint) -> bool:
    table = constraint.table
    keys = [
        key
        for key in table.constraints
        if isinstance(key, PrimaryKeyConstraint | UniqueConstraint)
    ]
    names = [column.name for column in constraint.columns]
    leading = [
        [column.name for column in index.columns][: len(names)]
        for index in [*table.indexes, *keys]
    ]
    return names in leading


def is_statement_timestamp(default: object) -> bool:
    return (
        isinstance(default, DefaultClause | ColumnDefault)
        and str(default.arg) == "statement_timestamp()"
    )


def is_stamped(table: Table, name: str) -> bool:
    column = table.columns.get(name)
    return (
        column is not None
        and is_statement_timestamp(column.server_default)
        and (name != "updated_at" or is_statement_timestamp(column.onupdate))
    )


def stores_values(enum_type: Enum) -> bool:
    members = enum_type.enum_class
    values = [member.value for member in members] if members else enum_type.enums
    return not enum_type.native_enum and enum_type.enums == values


def test_every_table_belongs_to_a_model() -> None:
    models = app_models()
    unmapped = sorted(
        set(Base.metadata.tables) - {model.table.name for model in models}
    )

    assert models
    assert unmapped == []


def test_primary_keys_are_uuid7_ids() -> None:
    models = app_models()
    wrong = [model.table.name for model in models if not has_uuid7_key(model.table)]

    assert models
    assert wrong == []


def test_mappers_return_server_defaults() -> None:
    models = app_models()
    lazy = [
        model.table.name for model in models if model.mapper.eager_defaults is not True
    ]

    assert models
    assert lazy == []


def test_foreign_keys_are_indexed() -> None:
    models = app_models()
    unindexed = [
        constraint.name
        for model in models
        for constraint in model.table.foreign_key_constraints
        if not is_indexed(constraint)
    ]

    assert models
    assert unindexed == []


def test_foreign_keys_end_in_id() -> None:
    models = app_models()
    misnamed = [
        f"{model.table.name}.{column.name}"
        for model in models
        for constraint in model.table.foreign_key_constraints
        for column in constraint.columns
        if not column.name.endswith("_id")
    ]

    assert models
    assert misnamed == []


def test_cross_module_foreign_keys_choose_on_delete() -> None:
    models = app_models()
    owners = {model.table.name: model.module for model in models}
    wrong = [
        constraint.name
        for model in models
        for constraint in model.table.foreign_key_constraints
        if owners.get(constraint.referred_table.name) != model.module
        and (
            model.module is None
            or (constraint.ondelete or "").upper() not in ON_DELETE_RULES
        )
    ]

    assert models
    assert wrong == []


def test_relationships_stay_inside_their_module() -> None:
    models = app_models()
    owners = {model.mapper: model.module for model in models}
    crossing = [
        f"{model.table.name}.{relationship.key}"
        for model in models
        for relationship in model.mapper.relationships
        if owners.get(relationship.mapper) != model.module
    ]

    assert models
    assert crossing == []


def test_relationships_raise_on_lazy_load() -> None:
    models = app_models()
    loading = [
        f"{model.table.name}.{relationship.key}"
        for model in models
        for relationship in model.mapper.relationships
        if relationship.lazy != "raise"
    ]

    assert models
    assert loading == []


def test_every_table_has_statement_timestamps() -> None:
    models = app_models()
    unstamped = [
        f"{model.table.name}.{name}"
        for model in models
        for name in ("created_at", "updated_at")
        if not is_stamped(model.table, name)
    ]

    assert models
    assert unstamped == []


def test_timestamps_have_time_zones() -> None:
    models = app_models()
    naive = [
        f"{model.table.name}.{column.name}"
        for model in models
        for column in model.table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]

    assert models
    assert naive == []


def test_enums_are_strings_stored_by_value() -> None:
    models = app_models()
    wrong = [
        f"{model.table.name}.{column.name}"
        for model in models
        for column in model.table.columns
        if isinstance(column.type, Enum) and not stores_values(column.type)
    ]

    assert models
    assert wrong == []
