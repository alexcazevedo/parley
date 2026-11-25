"""external_channel: a project-owned channel type and its ordered event feed

Adds `external` to the `channel_type` enum and the table `channel_events`:
one row per event of an external channel (`message.reply` for now), ordered
by `seq` (a BIGINT identity, unique), with the delivery columns the push to
`events_url` will use. `ix_channel_events_channel_id_seq` serves the feed
read (`channel_id = ? AND seq > ?`). `ALTER TYPE ... ADD VALUE` cannot run
inside a transaction block, hence the autocommit block (as in 0007).

Downgrade drops the table; the enum value stays (PostgreSQL cannot drop a
value from an enum type, see 0007).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'external'")
    op.create_table(
        "channel_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gave_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("seq", name="channel_events_seq_key"),
    )
    op.create_index("ix_channel_events_tenant_id", "channel_events", ["tenant_id"])
    op.create_index("ix_channel_events_channel_id_seq", "channel_events", ["channel_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_channel_events_channel_id_seq", table_name="channel_events")
    op.drop_index("ix_channel_events_tenant_id", table_name="channel_events")
    op.drop_table("channel_events")
    # The 'external' enum value stays: PostgreSQL cannot drop a value from an
    # enum type (see 0007).
