import asyncio

import structlog
from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.bootstrap import metadata
from app.core.config import Settings
from app.core.db import MIGRATION_LOCK_ID, psycopg_url
from app.core.logging import configure_logging

logger = structlog.stdlib.get_logger("alembic.env")


def unknown_revisions() -> list[str]:
    known = {script.revision for script in context.script.walk_revisions()}
    return sorted(set(context.get_context().get_current_heads()) - known)


def skips_a_database_ahead() -> bool:
    return context.get_x_argument(as_dictionary=True).get("ahead") == "skip"


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=metadata, compare_server_default=True
    )
    unknown = unknown_revisions() if skips_a_database_ahead() else []
    if unknown:
        logger.warning("migrations.skipped", unknown_revisions=unknown)
        return
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
