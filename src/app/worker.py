import asyncio

from procrastinate import PsycopgConnector

from app.bootstrap import build_resources, job_app
from app.core.config import Settings
from app.core.db import conninfo
from app.core.jobs.periodic import STALLED_AFTER_SECONDS
from app.core.jobs.tasks import observe_job, reporting_unknown_tasks
from app.core.logging import configure_logging
from app.core.sentry import configure_sentry, flush_sentry

WORKER_TIMEOUTS = "-c statement_timeout=60s -c idle_in_transaction_session_timeout=30s"
CONCURRENCY = 10
SHUTDOWN_GRACE_SECONDS = 30


async def work(settings: Settings) -> None:
    resources = build_resources(settings, WORKER_TIMEOUTS)
    connector = PsycopgConnector(
        conninfo=conninfo(settings.database_url),
        kwargs={"options": WORKER_TIMEOUTS, "connect_timeout": 5},
    )
    try:
        with job_app.replace_connector(connector), reporting_unknown_tasks():
            async with job_app.open_async():
                await job_app.run_worker_async(
                    concurrency=CONCURRENCY,
                    shutdown_graceful_timeout=SHUTDOWN_GRACE_SECONDS,
                    stalled_worker_timeout=STALLED_AFTER_SECONDS,
                    additional_context={"resources": resources},
                    worker_middleware=[observe_job],
                )
    finally:
        await resources.close()
        await flush_sentry()


def main() -> None:
    settings = Settings.model_validate({})
    configure_logging(settings)
    configure_sentry(settings)
    asyncio.run(work(settings))


if __name__ == "__main__":
    main()
