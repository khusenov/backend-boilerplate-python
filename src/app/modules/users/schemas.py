import uuid
from typing import Annotated

from pydantic import StringConstraints

from app.core.schemas import Email, Schema


class UserCreate(Schema):
    email: Email
    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=100,
            pattern=r"^[^\x00-\x1f\x7f]+$",
        ),
    ]


class UserDTO(Schema):
    id: uuid.UUID
    email: str
    active: bool
