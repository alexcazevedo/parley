"""agent knowledge switch: `agents.knowledge_enabled`

On, the agent gets the built-in `search_knowledge` tool and a knowledge
section (item titles only) in its system prompt. Default off.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("knowledge_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agents", "knowledge_enabled")
