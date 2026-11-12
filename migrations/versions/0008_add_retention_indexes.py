"""add retention indexes"""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"


def upgrade() -> None:
    op.create_index(
        op.f("ix_audit_events_created_at"), "audit_events", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_auth_sessions_expires_at"),
        "auth_sessions",
        ["expires_at"],
        unique=False,
    )
