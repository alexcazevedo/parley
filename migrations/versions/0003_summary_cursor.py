"""summary_cursor: rolling-summary bookmark on conversations

`summary_cursor` is nullable and holds the *count* of messages already folded
into `conversation.summary` (NULL/0 == "nothing summarized yet"). Messages
are append-only, so "every row at index >= cursor" is always exactly the
unsummarized tail — see app/conversations/summary.py.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("summary_cursor", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversations", "summary_cursor")
