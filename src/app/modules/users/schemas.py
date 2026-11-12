import uuid
from typing import ClassVar

from pydantic import MISSING

from app.core.jobs.queue import Event, Payload
from app.core.schemas import Email, Name, Schema, Timestamp
from app.core.security import Role


class UserCreate(Schema):
    email: Email
    name: Name


class UserUpdate(Schema):
    name: Name | MISSING = MISSING
    role: Role | MISSING = MISSING


class ProfileUpdate(Schema):
    name: Name | MISSING = MISSING


class EmailUpdate(Schema):
    email: Email


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


class SendWelcomeEmail(Payload):
    task_name: ClassVar[str] = "users.send_welcome_email"

    user_id: uuid.UUID


class SendEmailChangedNotice(Payload):
    task_name: ClassVar[str] = "users.send_email_changed_notice"

    old_email: str


class UserDeactivated(Event):
    event_type: ClassVar[str] = "users.deactivated.v1"

    user_id: uuid.UUID
