from app.core.errors import ConflictError, NotFoundError


class UserNotFoundError(NotFoundError):
    code = "users.not_found"


class EmailTakenError(ConflictError):
    code = "users.email_taken"
