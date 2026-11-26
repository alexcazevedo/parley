"""agent_boundaries: per-agent scope, never-list, refusal message, check switch

Four `agents` columns. Server defaults keep existing rows valid: no scope,
an empty never-list, no custom refusal message, reply check on.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("boundary_scope", sa.Text(), nullable=True))
    op.add_column(
        "agents",
        sa.Column(
            "boundary_never",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("agents", sa.Column("boundary_refusal_message", sa.Text(), nullable=True))
    op.add_column(
        "agents",
        sa.Column(
            "boundary_check_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "boundary_check_enabled")
    op.drop_column("agents", "boundary_refusal_message")
    op.drop_column("agents", "boundary_never")
    op.drop_column("agents", "boundary_scope")
