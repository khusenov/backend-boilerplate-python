import uuid
from importlib.resources import files
from typing import Annotated, ClassVar

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints

from app.core.jobs.queue import Payload
from app.core.schemas import Email, Name, Schema, Timestamp

COMMON_PASSWORDS = frozenset(
    files(__name__)
    .joinpath("common_passwords.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)


def check_uncommon(password: str) -> str:
    if password.casefold() in COMMON_PASSWORDS:
        raise ValueError("this password is too common")
    return password


Password = Annotated[str, Field(min_length=1, max_length=128)]
NewPassword = Annotated[
    str, Field(min_length=15, max_length=128), AfterValidator(check_uncommon)
]
Code = Annotated[str, StringConstraints(max_length=6, pattern=r"^[0-9]{6}$")]
Token = Annotated[str, StringConstraints(max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]


class Login(Schema):
    email: Email
    password: Password


class TokenRead(Schema):
    token: str
    expires_at: Timestamp


class PasswordSet(Schema):
    model_config = ConfigDict(hide_input_in_errors=True)

    password: NewPassword


class Signup(Schema):
    email: Email


class SignupConfirm(Schema):
    email: Email
    code: Code
    name: Name
    password: NewPassword


class EmailChange(Schema):
    email: Email
    password: Password


class EmailChangeConfirm(Schema):
    email: Email
    code: Code


class PasswordReset(Schema):
    email: Email


class PasswordResetConfirm(Schema):
    token: Token
    password: NewPassword


class PasswordChange(Schema):
    current_password: Password
    new_password: NewPassword


class SendCode(Payload):
    task_name: ClassVar[str] = "auth.send_code"

    code_id: uuid.UUID


class SendResetToken(Payload):
    task_name: ClassVar[str] = "auth.send_reset_token"

    reset_id: uuid.UUID


class SendPasswordChangedNotice(Payload):
    task_name: ClassVar[str] = "auth.send_password_changed_notice"

    user_id: uuid.UUID
