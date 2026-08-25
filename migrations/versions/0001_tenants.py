"""tenants

Revision ID: 0001
Revises:
Create Date: 2026-08-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("api_key_hash", name="uq_tenants_api_key_hash"),
    )
    op.create_index("ix_tenants_api_key_hash", "tenants", ["api_key_hash"])


def downgrade() -> None:
    op.drop_index("ix_tenants_api_key_hash", table_name="tenants")
    op.drop_table("tenants")
