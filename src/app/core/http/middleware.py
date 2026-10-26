import time
import uuid
from http import HTTPStatus

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.http.client_ip import client_address
from app.core.http.problems import HTTP_ERROR_CODES, problem_response

MAX_BODY_BYTES = 1024 * 1024
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}
STRICT_TRANSPORT_SECURITY = "max-age=63072000; includeSubDomains"

logger = structlog.stdlib.get_logger(__name__)


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid7())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/health/"):
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            client = scope.get("client")
            logger.info(
                "http.request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                client_ip=client[0] if client else None,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )


class ClientIpMiddleware:
    def __init__(self, app: ASGIApp, *, trusted_proxy_hops: int) -> None:
        self.app = app
        self.trusted_proxy_hops = trusted_proxy_hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        forwarded_for = ",".join(Headers(scope=scope).getlist("x-forwarded-for"))
        scope["client"] = client_address(
            forwarded_for, self.trusted_proxy_hops, scope.get("client")
        )
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.headers = dict(SECURITY_HEADERS)
        if hsts:
            self.headers["strict-transport-security"] = STRICT_TRANSPORT_SECURITY

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception:
            if not response_started:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                response = problem_response(status, HTTP_ERROR_CODES[status])
                await response(scope, receive, send)
            raise


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdecimal() and int(content_length) > MAX_BODY_BYTES:
            status = HTTPStatus.CONTENT_TOO_LARGE
            response = problem_response(status, HTTP_ERROR_CODES[status])
            await response(scope, receive, send)
            return
        received = 0

        async def receive_within_limit() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise HTTPException(HTTPStatus.CONTENT_TOO_LARGE)
            return message

        await self.app(scope, receive_within_limit, send)
