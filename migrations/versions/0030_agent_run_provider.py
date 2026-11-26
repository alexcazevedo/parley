"""agent_runs: the provider and model that answered

Both null on rows before this migration.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("provider", sa.String(), nullable=True))
    op.add_column("agent_runs", sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "model")
    op.drop_column("agent_runs", "provider")
