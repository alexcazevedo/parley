"""agent_run_boundary: record replies blocked by the boundary check

Two nullable `agent_runs` columns; null means the reply was not blocked.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("boundary_reason", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("blocked_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "blocked_text")
    op.drop_column("agent_runs", "boundary_reason")
