"""${message}"""

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}


def upgrade() -> None:
    ${"\n".join(line for line in upgrades.splitlines() if not line.lstrip().startswith("# ###")).strip() if upgrades else "pass"}
