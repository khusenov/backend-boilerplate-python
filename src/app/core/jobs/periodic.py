from datetime import UTC, datetime, timedelta

import sentry_sdk
import structlog
from procrastinate import Blueprint, JobContext
from procrastinate.exceptions import ConnectorException
from procrastinate.jobs import Job, Status
from sentry_sdk.crons import MonitorStatus, capture_checkin
from sentry_sdk.types import MonitorConfig
from sqlalchemy import text

from app.core import audit
from app.core.db import DELETE_BATCH_SIZE, delete_in_batches
from app.core.jobs.tasks import resources_of, retry_decision

EVERY_FIVE_MINUTES = "*/5 * * * *"
HOURLY = "0 * * * *"
HEARTBEAT_MONITOR: MonitorConfig = {
    "schedule": {"type": "crontab", "value": EVERY_FIVE_MINUTES},
    "checkin_margin": 2,
}
STALLED_AFTER_SECONDS = 60
AHEAD_OF_MODULE_JOBS = 1
JOB_RETENTION = timedelta(days=7)
LAST_OF_BATCH = text(
    "SELECT max(id) FROM (SELECT id FROM procrastinate_jobs WHERE id > :after "
    "ORDER BY id LIMIT :size) AS batch"
)
DELETE_FINISHED_JOBS = text(
    "DELETE FROM procrastinate_jobs WHERE id IN ("
    "SELECT job.id FROM procrastinate_jobs job "
    "JOIN procrastinate_events event ON event.job_id = job.id "
    "WHERE job.id > :after AND job.id <= :last "
    "AND job.status IN ('succeeded', 'failed', 'cancelled', 'aborted') "
    "GROUP BY job.id HAVING max(event.at) < :cutoff)"
)

blueprint = Blueprint()


@blueprint.periodic(cron=EVERY_FIVE_MINUTES)
@blueprint.task(name="heartbeat", priority=AHEAD_OF_MODULE_JOBS)
async def heartbeat(timestamp: int) -> None:
    capture_checkin(
        monitor_slug="worker-heartbeat",
        status=MonitorStatus.OK,
        monitor_config=HEARTBEAT_MONITOR,
    )


async def requeue_or_fail(context: JobContext, job: Job) -> None:
    manager = context.app.job_manager
    decision = retry_decision(context.app, job, TimeoutError("its worker stopped"))
    if decision is None:
        await manager.finish_job(job=job, status=Status.FAILED, delete_job=False)
        sentry_sdk.capture_message("Job failed: its worker stopped", level="error")
    else:
        await manager.retry_job(job, retry_at=decision.retry_at)


@blueprint.periodic(cron=EVERY_FIVE_MINUTES)
@blueprint.task(
    name="recover_stalled_jobs",
    queueing_lock="recover_stalled_jobs",
    pass_context=True,
    priority=AHEAD_OF_MODULE_JOBS,
)
async def recover_stalled_jobs(context: JobContext, timestamp: int) -> None:
    stalled = await context.app.job_manager.get_stalled_jobs(
        seconds_since_heartbeat=STALLED_AFTER_SECONDS
    )
    for job in stalled:
        with structlog.contextvars.bound_contextvars(job_id=job.id):
            try:
                await requeue_or_fail(context, job)
            except ConnectorException:
                sentry_sdk.capture_exception()


@blueprint.periodic(cron=HOURLY)
@blueprint.task(
    name="prune_audit_events", queueing_lock="prune_audit_events", pass_context=True
)
async def prune_audit_events(context: JobContext, timestamp: int) -> None:
    cutoff = datetime.now(UTC) - audit.RETENTION
    await delete_in_batches(
        resources_of(context).sessionmaker,
        audit.AuditEvent,
        audit.AuditEvent.created_at < cutoff,
    )


@blueprint.periodic(cron=HOURLY)
@blueprint.task(
    name="prune_finished_jobs", queueing_lock="prune_finished_jobs", pass_context=True
)
async def prune_finished_jobs(context: JobContext, timestamp: int) -> None:
    sessionmaker = resources_of(context).sessionmaker
    cutoff = datetime.now(UTC) - JOB_RETENTION
    after = 0
    while True:
        async with sessionmaker.begin() as session:
            last = await session.scalar(
                LAST_OF_BATCH, {"after": after, "size": DELETE_BATCH_SIZE}
            )
            if last is None:
                return
            await session.execute(
                DELETE_FINISHED_JOBS, {"after": after, "last": last, "cutoff": cutoff}
            )
        after = last
