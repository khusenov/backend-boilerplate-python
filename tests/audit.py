import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent

type AuditRow = tuple[
    uuid.UUID | None, str, uuid.UUID, str | None, str | None, dict[str, str]
]


async def audit_rows(session: AsyncSession) -> list[AuditRow]:
    events = await session.scalars(select(AuditEvent).order_by(AuditEvent.id))
    return [
        (
            event.actor_id,
            event.action,
            event.target_id,
            event.client_ip,
            event.request_id,
            event.changes,
        )
        for event in events
    ]
