"""mcp_servers and mcp_tools: a tenant's MCP servers and the tools discovered on them

Deleting a server deletes its tools (FK cascade). Indexes match the models.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    status = postgresql.ENUM("pending", "ok", "error", name="mcp_server_status")
    op.create_table(
        "mcp_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(24), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("auth_config_encrypted", sa.Text(), nullable=True),
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("skipped", postgresql.JSONB(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_id_name"),
    )
    op.create_index("ix_mcp_servers_tenant_id", "mcp_servers", ["tenant_id"])
    op.create_table(
        "mcp_tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("server_id", "name", name="uq_mcp_tools_server_id_name"),
    )
    op.create_index("ix_mcp_tools_tenant_id", "mcp_tools", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_tenant_id", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("ix_mcp_servers_tenant_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
    postgresql.ENUM(name="mcp_server_status").drop(op.get_bind(), checkfirst=True)
