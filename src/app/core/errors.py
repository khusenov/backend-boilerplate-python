from http import HTTPStatus
from typing import ClassVar


class AppError(Exception):
    status: ClassVar[HTTPStatus]
    code: ClassVar[str]


class NotFoundError(AppError):
    status = HTTPStatus.NOT_FOUND
