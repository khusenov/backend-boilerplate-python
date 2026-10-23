import logging
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from app.core.config import Settings

CREDENTIAL_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "csrf",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "dsn",
)
REDACTED = "[REDACTED]"
MAX_REDACTION_DEPTH = 32
SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def redact(value: object, depth: int) -> object:
    if depth >= MAX_REDACTION_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): redact_field(key, item, depth + 1)
            for key, item in mapping.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, depth + 1) for item in cast(Sequence[object], value)]
    return value


def redact_field(key: object, value: object, depth: int) -> object:
    name = str(key).lower().replace("-", "_")
    if any(part in name for part in CREDENTIAL_KEY_PARTS):
        return REDACTED
    return redact(value, depth)


def redact_credentials(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    return {key: redact_field(key, value, depth=0) for key, value in event_dict.items()}


def drop_color_message(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    event_dict.pop("color_message", None)
    return event_dict


def build_formatter(*, json_logs: bool) -> structlog.stdlib.ProcessorFormatter:
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            *SHARED_PROCESSORS,
            structlog.stdlib.ExtraAdder(),
            drop_color_message,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_credentials,
            renderer,
        ],
    )


def send_uvicorn_logs_to_root() -> None:
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.propagate = True


def turn_off_uvicorn_access_log() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False


def configure_logging(settings: Settings) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(json_logs=settings.app_env == "production"))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    send_uvicorn_logs_to_root()
    turn_off_uvicorn_access_log()
