import sentry_sdk
import structlog
from procrastinate import Blueprint, JobContext
from procrastinate.exceptions import ConnectorException
from procrastinate.jobs import Job, Status
from sentry_sdk.crons import MonitorStatus, capture_checkin
from sentry_sdk.types import MonitorConfig

from app.core.jobs.tasks import retry_decision

EVERY_FIVE_MINUTES = "*/5 * * * *"
HEARTBEAT_MONITOR: MonitorConfig = {
    "schedule": {"type": "crontab", "value": EVERY_FIVE_MINUTES},
    "checkin_margin": 2,
}
STALLED_AFTER_SECONDS = 60
AHEAD_OF_MODULE_JOBS = 1

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
