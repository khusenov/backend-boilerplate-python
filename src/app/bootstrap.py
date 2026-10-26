from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base, psycopg_url
from app.core.resources import Resources

API_TIMEOUTS = "-c statement_timeout=5s -c idle_in_transaction_session_timeout=30s"

metadata = Base.metadata


def build_resources(settings: Settings) -> Resources:
    engine = create_async_engine(
        psycopg_url(settings.database_url),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": API_TIMEOUTS, "connect_timeout": 5},
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return Resources(engine=engine, sessionmaker=sessionmaker)
