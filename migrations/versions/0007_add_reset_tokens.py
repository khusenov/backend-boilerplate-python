"""add reset tokens"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"


def upgrade() -> None:
    op.create_table(
        "auth_reset_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("seed", sa.LargeBinary(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_reset_tokens")),
        sa.UniqueConstraint("email", name=op.f("uq_auth_reset_tokens_email")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_reset_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_reset_tokens_user_id"),
        "auth_reset_tokens",
        ["user_id"],
        unique=False,
    )
