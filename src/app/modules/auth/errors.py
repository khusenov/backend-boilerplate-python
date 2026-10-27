from app.core.errors import TooManyRequestsError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    code = "auth.invalid_credentials"


class TooManyAttemptsError(TooManyRequestsError):
    code = "auth.too_many_attempts"
