"""manifest_items: allow the kinds `knowledge_entry` and `knowledge_qa`

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-26

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "kind IN ('agent', 'department', 'tool', 'channel')"
NEW = "kind IN ('agent', 'department', 'tool', 'channel', 'knowledge_entry', 'knowledge_qa')"


def upgrade() -> None:
    op.drop_constraint("ck_manifest_items_kind", "manifest_items", type_="check")
    op.create_check_constraint("ck_manifest_items_kind", "manifest_items", NEW)


def downgrade() -> None:
    op.execute("DELETE FROM manifest_items WHERE kind LIKE 'knowledge_%'")
    op.drop_constraint("ck_manifest_items_kind", "manifest_items", type_="check")
    op.create_check_constraint("ck_manifest_items_kind", "manifest_items", OLD)
