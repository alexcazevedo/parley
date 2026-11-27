"""session_recap memory type

Adds `session_recap` to the `memory_type` enum: the dated recap written when a
session ends (app/conversations/session_end.py). `ALTER TYPE ... ADD VALUE`
cannot run inside a transaction block, hence the autocommit block (as in 0007
and 0018); alembic commits what ran before it and opens a new transaction
after it.

Downgrade deletes the recap memories (an older version cannot read the type;
sessions pointing at them get `recap_memory_id` null through the foreign key).
The enum value stays: PostgreSQL cannot drop a value from an enum type.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-26

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE memory_type ADD VALUE IF NOT EXISTS 'session_recap'")


def downgrade() -> None:
    # Compared as text: the value cannot be named as an enum literal in the
    # same transaction that added it, and text works either way.
    op.execute("DELETE FROM memories WHERE type::text = 'session_recap'")
