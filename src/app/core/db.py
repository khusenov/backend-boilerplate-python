import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import URL, DateTime, MetaData, func, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

MIGRATION_LOCK_ID = 7_243_015_001


def psycopg_url(database_url: SecretStr) -> URL:
    raw = database_url.get_secret_value()
    return make_url(raw).set(drivername="postgresql+psycopg")


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
        }
    )
    type_annotation_map: Mapping[type[Any], TypeEngine[Any]] = {
        datetime: DateTime(timezone=True)
    }
    __mapper_args__: Mapping[str, Any] = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid7,
        server_default=func.uuidv7(),
        insert_sentinel=True,
        sort_order=-1,
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.statement_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.statement_timestamp(),
        onupdate=func.statement_timestamp(),
    )
