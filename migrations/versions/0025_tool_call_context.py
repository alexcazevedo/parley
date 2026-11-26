"""tool_executions: `call_id` and `context` (who the call was for)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_executions",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "tool_executions",
        sa.Column("context", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tool_executions", "context")
    op.drop_column("tool_executions", "call_id")
