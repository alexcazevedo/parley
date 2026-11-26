"""manifest_items: the items a tenant's manifest manages

One row per (tenant, kind, key) and per (tenant, kind, item_id).
`ix_manifest_items_tenant_id` matches the `index=True` of `TenantScoped`.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manifest_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "secret_names",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "kind", "key", name="uq_manifest_items_tenant_kind_key"),
        sa.UniqueConstraint(
            "tenant_id", "kind", "item_id", name="uq_manifest_items_tenant_kind_item"
        ),
        sa.CheckConstraint(
            "kind IN ('agent', 'department', 'tool', 'channel')",
            name="ck_manifest_items_kind",
        ),
    )
    op.create_index("ix_manifest_items_tenant_id", "manifest_items", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_manifest_items_tenant_id", table_name="manifest_items")
    op.drop_table("manifest_items")
