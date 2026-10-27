from typing import Annotated

from pydantic import Field

from app.core.schemas import Email, Schema, Timestamp


class Login(Schema):
    email: Email
    password: Annotated[str, Field(min_length=1, max_length=128)]


class TokenRead(Schema):
    token: str
    expires_at: Timestamp
