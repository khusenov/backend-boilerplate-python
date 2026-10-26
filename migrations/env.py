import asyncio

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.bootstrap import metadata
from app.core.config import Settings
from app.core.db import MIGRATION_LOCK_ID, psycopg_url
from app.core.logging import configure_logging


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=metadata, compare_server_default=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate(settings: Settings) -> None:
    engine = create_async_engine(
        psycopg_url(settings.database_url),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID}
        )
        await connection.execute(text("SET lock_timeout = '5s'"))
        await connection.commit()
        await connection.run_sync(run_migrations)
    await engine.dispose()


settings = Settings.model_validate({})
configure_logging(settings)
asyncio.run(migrate(settings))
