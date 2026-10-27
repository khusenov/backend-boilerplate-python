from collections.abc import Callable, Coroutine, Mapping, Sequence
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.core.errors import AppError
from app.core.schemas import Schema

PROBLEM_JSON = "application/problem+json"
URI_FRAGMENT_SAFE = "/?:@!$&'()*+,;="
HTTP_ERROR_CODES = {
    HTTPStatus.BAD_REQUEST: "bad_request",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
    HTTPStatus.CONTENT_TOO_LARGE: "content_too_large",
    HTTPStatus.UNPROCESSABLE_CONTENT: "validation_failed",
    HTTPStatus.INTERNAL_SERVER_ERROR: "internal_server_error",
    HTTPStatus.SERVICE_UNAVAILABLE: "service_unavailable",
}


class InvalidParam(Schema):
    pointer: str | None = None
    parameter: str | None = None
    detail: str


class Problem(Schema):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str
    request_id: str
    errors: list[InvalidParam] | None = None


def problem_response(
    status: HTTPStatus,
    code: str,
    *,
    detail: str | None = None,
    errors: list[InvalidParam] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    problem = Problem(
        title=status.phrase,
        status=status.value,
        detail=detail,
        code=code,
        request_id=structlog.contextvars.get_contextvars().get("request_id", ""),
        errors=errors,
    )
    return Response(
        problem.model_dump_json(exclude_none=True),
        status_code=status.value,
        headers=headers,
        media_type=PROBLEM_JSON,
    )


def json_pointer(path: Sequence[object]) -> str:
    parts = (str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return "#" + quote("".join(f"/{part}" for part in parts), safe=URI_FRAGMENT_SAFE)


def body_path(loc: Sequence[Any], body: Any, *, missing: bool) -> list[Any]:
    path: list[Any] = []
    node = body
    for position, segment in enumerate(loc, start=1):
        try:
            node = node[segment]
        except LookupError, TypeError:
            if not missing or position < len(loc):
                continue
        path.append(segment)
    return path


def invalid_param(error: Mapping[str, Any], body: Any) -> InvalidParam:
    source, *loc = error["loc"]
    if source != "body":
        return InvalidParam(parameter=".".join(map(str, loc)), detail=error["msg"])
    if error["type"] == "json_invalid":
        return InvalidParam(pointer="#", detail=error["msg"])
    path = body_path(loc, body, missing=error["type"] == "missing")
    return InvalidParam(pointer=json_pointer(path), detail=error["msg"])


async def handle_validation_error(
    _request: Request, error: RequestValidationError
) -> Response:
    errors = [invalid_param(item, error.body) for item in error.errors()]
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    return problem_response(status, HTTP_ERROR_CODES[status], errors=errors)


async def handle_http_exception(_request: Request, error: HTTPException) -> Response:
    status = HTTPStatus(error.status_code)
    code = HTTP_ERROR_CODES.get(status, status.name.lower())
    return problem_response(status, code, headers=error.headers)


async def handle_app_error(_request: Request, error: AppError) -> Response:
    return problem_response(
        error.status, error.code, detail=str(error) or None, headers=error.headers
    )


type ExceptionHandler = Callable[[Request, Any], Coroutine[Any, Any, Response]]

EXCEPTION_HANDLERS: dict[int | type[Exception], ExceptionHandler] = {
    HTTPException: handle_http_exception,
    RequestValidationError: handle_validation_error,
    AppError: handle_app_error,
}
