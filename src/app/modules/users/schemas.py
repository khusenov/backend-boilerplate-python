import uuid
from typing import Annotated

from pydantic import MISSING, StringConstraints

from app.core.schemas import Email, Schema, Timestamp
from app.core.security import Role

Name = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


class UserCreate(Schema):
    email: Email
    name: Name


class UserUpdate(Schema):
    name: Name | MISSING = MISSING
    role: Role | MISSING = MISSING


class ProfileUpdate(Schema):
    name: Name | MISSING = MISSING


class UserRead(Schema):
    id: uuid.UUID
    email: str
    name: str
    role: Role
    created_at: Timestamp
    updated_at: Timestamp


class UserPage(Schema):
    items: list[UserRead]
    next_cursor: str | None


class UserDTO(Schema):
    id: uuid.UUID
    email: str
    role: Role
    active: bool
