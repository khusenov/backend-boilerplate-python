import io
import json
import logging
import secrets
import uuid
from contextlib import redirect_stdout
from typing import Literal

import pytest
import structlog
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.core.logging import MAX_REDACTION_DEPTH, build_formatter, redact_credentials
from app.main import create_app
from tests.logs import json_lines

logger = structlog.stdlib.get_logger(__name__)
router = APIRouter()


@router.post("/signup")
async def signup(body: dict[str, str]) -> None:
    logger.info("signup.received", email=body["email"], password=body["password"])


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(router)
    return app


async def test_json_line_has_request_id_and_redacted_password(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    password = secrets.token_urlsafe(16)

    response = await client.post(
        "/signup", json={"email": "ada@example.com", "password": password}
    )

    lines = json_lines(caplog)
    signup = next(line for line in lines if line["event"] == "signup.received")
    assert signup["request_id"] == response.headers["x-request-id"]
    assert signup["password"] == "[REDACTED]"
    assert signup["email"] == "ada@example.com"
    assert password not in str(lines)


def test_production_writes_json_lines(production_settings: Settings) -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        create_app(production_settings)
        logger.info("app.started")

    line = json.loads(stdout.getvalue())
    assert set(line) == {"event", "level", "logger", "timestamp"}
    assert (line["event"], line["level"], line["logger"]) == (
        "app.started",
        "info",
        __name__,
    )
    assert line["timestamp"].endswith("Z")


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_other_environments_write_console_lines(
    settings: Settings, app_env: Literal["local", "test"]
) -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        create_app(settings.model_copy(update={"app_env": app_env}))
        logger.info("app.started", password="local-password")

    output = stdout.getvalue()
    assert "[info     ] app.started" in output
    assert "password=[REDACTED]" in output
    assert "\x1b[" not in output


def test_credentials_are_redacted_at_any_depth() -> None:
    event = {
        "event": "user.updated",
        "Authorization": "Bearer abc",
        "user": {"email": "ada@example.com", "new_password": "pw"},
        "changes": [{"field": "role"}, {"api-key": "key"}],
        "attempts": ({"session_token": "token"},),
    }

    assert redact_credentials(None, "info", event) == {
        "event": "user.updated",
        "Authorization": "[REDACTED]",
        "user": {"email": "ada@example.com", "new_password": "[REDACTED]"},
        "changes": [{"field": "role"}, {"api-key": "[REDACTED]"}],
        "attempts": [{"session_token": "[REDACTED]"}],
    }


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "client_secret",
        "refresh_token",
        "Authorization",
        "Set-Cookie",
        "session_id",
        "X-CSRF",
        "X-API-Key",
        "apikey",
        "credentials",
        "private_key",
        "sentry_dsn",
    ],
)
def test_each_credential_key_is_redacted(key: str) -> None:
    assert redact_credentials(None, "info", {key: "value"}) == {key: "[REDACTED]"}


def test_values_past_the_depth_limit_are_redacted() -> None:
    mapping: dict[str, object] = {}
    mapping["self"] = mapping
    sequence: list[object] = []
    sequence.append(sequence)
    expected_mapping: object = "[REDACTED]"
    expected_sequence: object = "[REDACTED]"
    for _ in range(MAX_REDACTION_DEPTH):
        expected_mapping = {"self": expected_mapping}
        expected_sequence = [expected_sequence]

    event = {"mapping": mapping, "sequence": sequence}
    assert redact_credentials(None, "info", event) == {
        "mapping": expected_mapping,
        "sequence": expected_sequence,
    }


def test_library_records_keep_extra_fields_and_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("library").warning(
        "job.deferred", extra={"job_id": 7, "password": "pw"}
    )

    (line,) = json_lines(caplog)
    assert (line["event"], line["job_id"], line["password"]) == (
        "job.deferred",
        7,
        "[REDACTED]",
    )


def test_mapping_keys_become_strings(caplog: pytest.LogCaptureFixture) -> None:
    user_id = uuid.uuid7()
    logging.getLogger("library").warning(
        "users.notified", extra={"results": {user_id: "sent"}, "password": "pw"}
    )

    (line,) = json_lines(caplog)
    assert line["results"] == {str(user_id): "sent"}
    assert line["password"] == "[REDACTED]"


@pytest.mark.parametrize("json_logs", [True, False])
def test_tracebacks_leave_out_local_variables(
    caplog: pytest.LogCaptureFixture, json_logs: bool
) -> None:
    attempt = secrets.token_urlsafe(16)
    with pytest.raises(RuntimeError) as caught:
        raise RuntimeError("sign-in failed")
    logging.getLogger("app").error("sign_in.failed", exc_info=caught.value)

    (record,) = caplog.records
    output = build_formatter(json_logs=json_logs).format(record)
    assert attempt not in output
    assert "RuntimeError: sign-in failed" in output


def test_uvicorn_lines_reach_stdout_without_access_lines(
    production_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    uvicorn_output = io.StringIO()
    for name in ("uvicorn", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        handler = logging.StreamHandler(uvicorn_output)
        monkeypatch.setattr(uvicorn_logger, "handlers", [handler])
        monkeypatch.setattr(uvicorn_logger, "propagate", False)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        create_app(production_settings)
        logging.getLogger("uvicorn.error").info(
            "Started server process [%d]",
            42,
            extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
        )

    line = json.loads(stdout.getvalue())
    assert set(line) == {"event", "level", "logger", "timestamp"}
    assert line["event"] == "Started server process [42]"
    assert uvicorn_output.getvalue() == ""
    assert not logging.getLogger("uvicorn.access").hasHandlers()
