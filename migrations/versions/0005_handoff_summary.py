"""handoff_summary: nullable operator-brief column on handoffs

`handoffs.summary` holds the best-effort, LLM-written operator brief produced
at handoff time (see app/handoff/summarizer.py). Nullable and no server
default: a failed/slow summarizer must never block the escalation itself, so
existing and future rows are allowed to have no summary at all.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "handoffs",
        sa.Column("summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("handoffs", "summary")
