import asyncio
from concurrent.futures import ThreadPoolExecutor

from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base, psycopg_url
from app.core.resources import Resources
from app.modules.auth import models as auth_models
from app.modules.auth import public as auth
from app.modules.users import models as users_models

API_TIMEOUTS = "-c statement_timeout=5s -c idle_in_transaction_session_timeout=30s"
HASHING_THREADS = 4

MODELS = (auth_models, users_models)
metadata = Base.metadata


def build_resources(settings: Settings) -> Resources:
    engine = create_async_engine(
        psycopg_url(settings.database_url),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": API_TIMEOUTS, "connect_timeout": 5},
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return Resources(
        engine=engine,
        sessionmaker=sessionmaker,
        limiter=MovingWindowRateLimiter(MemoryStorage()),
        hashing=ThreadPoolExecutor(HASHING_THREADS, thread_name_prefix="hashing"),
        hashing_slots=asyncio.Semaphore(HASHING_THREADS),
        authenticate=auth.authenticate,
    )
