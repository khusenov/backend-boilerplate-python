"""add procrastinate schema"""

from pathlib import Path

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
procrastinate_version: str = "3.10.0"


def upgrade() -> None:
    schema = Path(__file__).parents[1] / f"procrastinate-{procrastinate_version}.sql"
    op.execute(schema.read_text(encoding="utf-8"))
