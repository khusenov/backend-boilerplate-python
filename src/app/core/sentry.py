import asyncio

import sentry_sdk
import structlog
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_PII_DENYLIST, EventScrubber
from sentry_sdk.types import Event, Hint

from app.core.config import Settings

TAGGED_CONTEXT = ("request_id", "job_id")


def tag_context(event: Event, _hint: Hint) -> Event:
    context = structlog.contextvars.get_contextvars()
    for key in TAGGED_CONTEXT:
        if key in context:
            event.setdefault("tags", {})[key] = str(context[key])
    return event


def configure_sentry(settings: Settings) -> None:
    if settings.sentry_dsn is None:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment or settings.app_env,
        release=settings.git_sha,
        max_request_body_size="never",
        include_local_variables=False,
        event_scrubber=EventScrubber(
            pii_denylist=[*DEFAULT_PII_DENYLIST, "cf-connecting-ip", "true-client-ip"]
        ),
        in_app_include=["app"],
        integrations=[LoggingIntegration(level=None, event_level=None)],
        before_send=tag_context,
    )


async def flush_sentry() -> None:
    await asyncio.to_thread(sentry_sdk.flush)
