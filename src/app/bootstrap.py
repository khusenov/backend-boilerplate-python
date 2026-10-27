import asyncio
from concurrent.futures import ThreadPoolExecutor

from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base, psycopg_url
from app.core.email import SmtpEmailSender
from app.core.jobs import periodic
from app.core.jobs.queue import JobQueue, build_job_app
from app.core.resources import Resources
from app.modules.auth import models as auth_models
from app.modules.auth import public as auth
from app.modules.users import models as users_models
from app.modules.users import tasks as users_tasks

API_TIMEOUTS = "-c statement_timeout=5s -c idle_in_transaction_session_timeout=30s"
HASHING_THREADS = 4

MODELS = (auth_models, users_models)
BLUEPRINTS = (periodic.blueprint, users_tasks.blueprint)
metadata = Base.metadata
job_app = build_job_app(BLUEPRINTS)


def build_resources(settings: Settings, timeouts: str = API_TIMEOUTS) -> Resources:
    engine = create_async_engine(
        psycopg_url(settings.database_url),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": timeouts, "connect_timeout": 5},
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return Resources(
        engine=engine,
        sessionmaker=sessionmaker,
        jobs=JobQueue(job_app),
        email=SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            tls=settings.smtp_tls,
            from_address=settings.email_from,
        ),
        limiter=MovingWindowRateLimiter(MemoryStorage()),
        hashing=ThreadPoolExecutor(HASHING_THREADS, thread_name_prefix="hashing"),
        hashing_slots=asyncio.Semaphore(HASHING_THREADS),
        authenticate=auth.authenticate,
    )
