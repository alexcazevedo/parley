"""categories: the tenant's list of subjects, and a category on each conversation

`categories` holds one list per tenant (unique name per tenant). Conversations
get `category_id` (nullable, SET NULL when the category is deleted) and
`category_locked` (true once a human chose the category; the classifier then
leaves it alone).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-20

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_categories_tenant_name"),
    )
    op.create_index("ix_categories_tenant_id", "categories", ["tenant_id"])
    op.add_column(
        "conversations",
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_conversations_category_id", "conversations", ["category_id"])
    op.add_column(
        "conversations",
        sa.Column("category_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("conversations", "category_locked")
    op.drop_index("ix_conversations_category_id", table_name="conversations")
    op.drop_column("conversations", "category_id")
    op.drop_index("ix_categories_tenant_id", table_name="categories")
    op.drop_table("categories")
