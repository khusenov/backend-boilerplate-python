import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Credential(Base):
    __tablename__ = "auth_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    password_hash: Mapped[str]


class LoginSession(Base):
    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(unique=True)
    expires_at: Mapped[datetime]


class VerificationCode(Base):
    __tablename__ = "auth_verification_codes"

    email: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seed: Mapped[bytes]
    code_mac: Mapped[bytes | None]
    expires_at: Mapped[datetime]


class ResetToken(Base):
    __tablename__ = "auth_reset_tokens"

    email: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seed: Mapped[bytes]
    token_hash: Mapped[bytes | None] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
