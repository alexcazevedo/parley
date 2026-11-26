"""manifest_items: allow the kind `mcp_server`

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-26

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "kind IN ('agent', 'department', 'tool', 'channel', 'knowledge_entry', 'knowledge_qa')"
NEW = (
    "kind IN ('agent', 'department', 'tool', 'channel', 'knowledge_entry', 'knowledge_qa', "
    "'mcp_server')"
)


def upgrade() -> None:
    op.drop_constraint("ck_manifest_items_kind", "manifest_items", type_="check")
    op.create_check_constraint("ck_manifest_items_kind", "manifest_items", NEW)


def downgrade() -> None:
    op.execute("DELETE FROM manifest_items WHERE kind = 'mcp_server'")
    op.drop_constraint("ck_manifest_items_kind", "manifest_items", type_="check")
    op.create_check_constraint("ck_manifest_items_kind", "manifest_items", OLD)
