"""conversation_sessions, and the four session settings on agents

A conversation is split into sessions by silence (app/conversations/sessions.py).
Existing conversations get no row here: their first row is created on first
need. Indexes match the model.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "session_memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "agents",
        sa.Column("session_gap_hours", sa.Integer(), nullable=False, server_default=sa.text("48")),
    )
    op.add_column(
        "agents",
        sa.Column("session_recap_limit", sa.Integer(), nullable=False, server_default=sa.text("3")),
    )
    op.add_column(
        "agents",
        sa.Column(
            "session_recap_max_age_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
        ),
    )
    op.create_table(
        "conversation_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary_snapshot", sa.Text(), nullable=True),
        sa.Column(
            "recap_memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_conversation_sessions_tenant_id", "conversation_sessions", ["tenant_id"]
    )
    op.create_index(
        "ix_conversation_sessions_conversation_id", "conversation_sessions", ["conversation_id"]
    )
    op.create_index(
        "ix_conversation_sessions_contact_id", "conversation_sessions", ["contact_id"]
    )
    op.create_index(
        "uq_conversation_sessions_open",
        "conversation_sessions",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_sessions_open", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_contact_id", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_conversation_id", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_tenant_id", table_name="conversation_sessions")
    op.drop_table("conversation_sessions")
    op.drop_column("agents", "session_recap_max_age_days")
    op.drop_column("agents", "session_recap_limit")
    op.drop_column("agents", "session_gap_hours")
    op.drop_column("agents", "session_memory_enabled")
