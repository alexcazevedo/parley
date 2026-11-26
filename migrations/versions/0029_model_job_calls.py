"""model_job_calls: one row per call of an auxiliary model job

Deleting a conversation deletes its rows (FK cascade). Indexes match the model.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_job_calls",
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
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job", sa.String(32), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("handed_over", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_model_job_calls_tenant_id", "model_job_calls", ["tenant_id"])
    op.create_index(
        "ix_model_job_calls_tenant_conversation_created",
        "model_job_calls",
        ["tenant_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_job_calls_tenant_conversation_created", table_name="model_job_calls")
    op.drop_index("ix_model_job_calls_tenant_id", table_name="model_job_calls")
    op.drop_table("model_job_calls")
