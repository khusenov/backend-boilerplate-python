import uuid
from collections.abc import Mapping
from datetime import timedelta

import structlog
from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

RETENTION = timedelta(days=365)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index(None, "created_at"),)

    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str]
    target_id: Mapped[uuid.UUID]
    client_ip: Mapped[str | None]
    request_id: Mapped[str | None]
    changes: Mapped[dict[str, str]] = mapped_column(JSONB)


def record(
    session: AsyncSession,
    action: str,
    *,
    actor_id: uuid.UUID | None,
    target_id: uuid.UUID,
    client_ip: str | None,
    changes: Mapping[str, str],
) -> None:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    session.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            target_id=target_id,
            client_ip=client_ip,
            request_id=request_id,
            changes=dict(changes),
        )
    )
