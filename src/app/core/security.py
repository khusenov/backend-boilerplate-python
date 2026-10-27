import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class Permission(StrEnum):
    AUTH_LOG_OUT = "auth:log_out"
    USERS_READ = "users:read"
    USERS_READ_ALL = "users:read_all"
    USERS_WRITE = "users:write"
    USERS_WRITE_SELF = "users:write_self"


ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.MEMBER: frozenset(
        {Permission.AUTH_LOG_OUT, Permission.USERS_READ, Permission.USERS_WRITE_SELF}
    ),
}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    role: Role

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


type Authenticate = Callable[[AsyncSession, str], Awaitable[Principal | None]]
