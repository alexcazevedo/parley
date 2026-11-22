"""waiting line: when a conversation began waiting for a human, and who claimed it

`waiting_since` is the start of the current handoff; `claimed_by`/`claimed_at`
are written by the claim endpoints. `ix_conversations_waiting` serves the
lines. Existing `HUMAN_HANDOFF` conversations get `waiting_since` from their
newest unresolved handoff, else now().

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("conversations", sa.Column("claimed_by", sa.Text(), nullable=True))
    op.add_column(
        "conversations", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """
        UPDATE conversations c
        SET waiting_since = COALESCE(
            (
                SELECT h.created_at FROM handoffs h
                WHERE h.conversation_id = c.id AND h.resolved_at IS NULL
                ORDER BY h.created_at DESC
                LIMIT 1
            ),
            now()
        )
        WHERE c.state = 'HUMAN_HANDOFF'
        """
    )
    op.create_index(
        "ix_conversations_waiting",
        "conversations",
        ["tenant_id", "waiting_since"],
        postgresql_where=sa.text("state = 'HUMAN_HANDOFF'"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_waiting", table_name="conversations")
    op.drop_column("conversations", "claimed_at")
    op.drop_column("conversations", "claimed_by")
    op.drop_column("conversations", "waiting_since")
