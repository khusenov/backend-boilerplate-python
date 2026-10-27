from typing import Annotated

from pydantic import ConfigDict, Field

from app.core.schemas import Email, Schema, Timestamp


class Login(Schema):
    email: Email
    password: Annotated[str, Field(min_length=1, max_length=128)]


class TokenRead(Schema):
    token: str
    expires_at: Timestamp


class PasswordSet(Schema):
    model_config = ConfigDict(hide_input_in_errors=True)

    password: Annotated[str, Field(min_length=15, max_length=128)]
