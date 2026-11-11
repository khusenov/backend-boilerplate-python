from app.core.errors import ForbiddenError, TooManyRequestsError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    code = "auth.invalid_credentials"


class TooManyAttemptsError(TooManyRequestsError):
    code = "auth.too_many_attempts"


class InvalidCodeError(ForbiddenError):
    code = "auth.invalid_code"


class WrongPasswordError(ForbiddenError):
    code = "auth.wrong_password"
