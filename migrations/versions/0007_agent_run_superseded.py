"""agent_run_superseded: 'superseded' status for barge-in

A turn that is interrupted by a newer inbound message drops its unsent reply
at a checkpoint (see app/conversations/turn.py) and marks its `AgentRun`
`superseded` — observable, and deliberately not an error. `ALTER TYPE ... ADD
VALUE` cannot run inside a transaction block, hence the autocommit block.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent_run_status ADD VALUE IF NOT EXISTS 'superseded'")


def downgrade() -> None:
    # No-op: PostgreSQL cannot drop a value from an enum type. Rolling back
    # would mean recreating the type and rewriting every dependent column,
    # which is not worth it for an additive, unused-on-downgrade value.
    pass
