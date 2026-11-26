"""knowledge embeddings: `knowledge_embeddings`, only where pgvector is available

One vector per passage (`passage_id` primary key, deleted with the passage),
with the tenant and the model that produced it. The column has no fixed
dimension. The table is outside the ORM: on a server without the `vector`
extension this migration does nothing and search stays keyword only; the
re-index command (`python -m app.knowledge.reindex`) creates it later.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    available = bind.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).scalar()
    if not available:
        return
    try:
        with bind.begin_nested():
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except DBAPIError:
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_embeddings (
            passage_id uuid PRIMARY KEY REFERENCES knowledge_passages(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            model text NOT NULL,
            embedding vector NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_embeddings_tenant_model "
        "ON knowledge_embeddings (tenant_id, model)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_embeddings")
