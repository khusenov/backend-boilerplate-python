import uuid
from importlib.resources import files
from typing import Annotated, ClassVar

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints

from app.core.jobs.queue import Message
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


class SendCode(Message):
    task_name: ClassVar[str] = "auth.send_code"

    code_id: uuid.UUID
