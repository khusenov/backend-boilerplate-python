import json
from typing import Any

import pytest

from app.core.logging import build_formatter

JSON_FORMATTER = build_formatter(json_logs=True)


def json_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(JSON_FORMATTER.format(record)) for record in caplog.records]
