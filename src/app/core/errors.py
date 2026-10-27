from collections.abc import Mapping
from http import HTTPStatus
from typing import ClassVar


class AppError(Exception):
    status: ClassVar[HTTPStatus]
    code: ClassVar[str]
    headers: ClassVar[Mapping[str, str]] = {}


class UnauthorizedError(AppError):
    status = HTTPStatus.UNAUTHORIZED
    headers: ClassVar[Mapping[str, str]] = {"www-authenticate": "Bearer"}


class ForbiddenError(AppError):
    status = HTTPStatus.FORBIDDEN


class NotFoundError(AppError):
    status = HTTPStatus.NOT_FOUND


class ConflictError(AppError):
    status = HTTPStatus.CONFLICT


class TooManyRequestsError(AppError):
    status = HTTPStatus.TOO_MANY_REQUESTS


class NotAuthenticatedError(UnauthorizedError):
    code = "unauthenticated"


class UntrustedOriginError(ForbiddenError):
    code = "untrusted_origin"
