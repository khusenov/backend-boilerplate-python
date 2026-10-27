from enum import StrEnum

from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.security import Role


class UserStatus(StrEnum):
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class User(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    role: Mapped[Role] = mapped_column(server_default=Role.MEMBER)
    status: Mapped[UserStatus] = mapped_column(default=UserStatus.ACTIVE)

    @property
    def active(self) -> bool:
        return self.status == UserStatus.ACTIVE
