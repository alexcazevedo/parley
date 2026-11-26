"""tenant_secrets: named, encrypted secrets a tenant stores

One row per (tenant, name); the value is Fernet-encrypted in `value_encrypted`.
`ix_tenant_secrets_tenant_id` matches the `index=True` of `TenantScoped`.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_tenant_secrets_tenant_name"),
    )
    op.create_index("ix_tenant_secrets_tenant_id", "tenant_secrets", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_secrets_tenant_id", table_name="tenant_secrets")
    op.drop_table("tenant_secrets")
