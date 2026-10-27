import logging
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast, override

import sentry_sdk
import structlog
from procrastinate import App, BaseRetryStrategy, Blueprint, JobContext, RetryDecision
from procrastinate.exceptions import TaskNotFound
from procrastinate.jobs import Job
from procrastinate.middleware import AsyncCallNext
from procrastinate.tasks import Task
from procrastinate.worker import Worker

from app.core.jobs.queue import Message
from app.core.resources import Resources

type Handler[P: Message] = Callable[[JobContext, P], Awaitable[None]]

WORKER_LOGGER = logging.getLogger("procrastinate.worker")


@dataclass(frozen=True, kw_only=True)
class CappedExponentialRetry(BaseRetryStrategy):
    max_attempts: int
    first_delay: timedelta
    max_delay: timedelta

    def delay(self, attempts: int) -> timedelta | None:
        if attempts + 1 >= self.max_attempts:
            return None
        return min(self.max_delay, self.first_delay * 2**attempts)

    @override
    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        delay = self.delay(job.attempts)
        if delay is None:
            return None
        return RetryDecision(retry_at=datetime.now(UTC) + delay)


RETRY = CappedExponentialRetry(
    max_attempts=14, first_delay=timedelta(seconds=2), max_delay=timedelta(hours=1)
)


def resources_of(context: JobContext) -> Resources:
    resources = cast(dict[str, object], context.additional_context).get("resources")
    if not isinstance(resources, Resources):
        raise TypeError("the worker was started without Resources")
    return resources


def task[P: Message](
    blueprint: Blueprint, payload_type: type[P], *, queueing_lock: str | None = None
) -> Callable[[Handler[P]], Handler[P]]:
    def register(handler: Handler[P]) -> Handler[P]:
        async def run(context: JobContext, **payload: object) -> None:
            await handler(context, payload_type.model_validate(payload))

        blueprint.task(
            name=payload_type.task_name,
            pass_context=True,
            retry=RETRY,
            queueing_lock=queueing_lock,
        )(run)
        return handler

    return register


def retry_decision(app: App, job: Job, error: BaseException) -> RetryDecision | None:
    job_task = cast(dict[str, Task[..., object, ...]], app.tasks).get(job.task_name)
    retry = job_task.get_retry_exception(exception=error, job=job) if job_task else None
    return retry.retry_decision if retry else None


def is_final(context: JobContext, error: BaseException) -> bool:
    return retry_decision(context.app, context.job, error) is None


async def observe_job(
    call_next: AsyncCallNext, context: JobContext, _worker: Worker
) -> object:
    structlog.contextvars.bind_contextvars(job_id=context.job.id)
    with sentry_sdk.isolation_scope():
        try:
            return await call_next()
        except BaseException as error:
            if is_final(context, error):
                sentry_sdk.capture_exception(error)
            raise


class UnknownTaskReporter(logging.Handler):
    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.__dict__.get("action") != "task_not_found":
            return
        job = cast(dict[str, object], record.__dict__["job"])
        with structlog.contextvars.bound_contextvars(job_id=job["id"]):
            sentry_sdk.capture_exception(
                TaskNotFound(f"no task named {job['task_name']}")
            )


@contextmanager
def reporting_unknown_tasks() -> Generator[None]:
    reporter = UnknownTaskReporter()
    WORKER_LOGGER.addHandler(reporter)
    try:
        yield
    finally:
        WORKER_LOGGER.removeHandler(reporter)
