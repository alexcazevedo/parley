"""knowledge: a tenant's knowledge items and their searchable passages

`knowledge_items` holds entries, question-and-answer pairs and documents
(the document columns are used by a later change). A title is unique per
tenant and kind except for documents (partial unique index).
`knowledge_passages` are written by the worker (app/knowledge/ingest.py);
`search_vector` is generated from `search_text` and GIN-indexed. Deleting an
item deletes its passages (FK cascade). Indexes match the models.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    kind = postgresql.ENUM("entry", "qa", "document", name="knowledge_item_kind")
    status = postgresql.ENUM("processing", "ready", "failed", name="knowledge_item_status")
    op.create_table(
        "knowledge_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", kind, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", status, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("source_key", sa.String(), nullable=True),
        sa.Column("source_mime", sa.String(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_knowledge_items_tenant_id", "knowledge_items", ["tenant_id"])
    op.create_index(
        "uq_knowledge_items_tenant_kind_title",
        "knowledge_items",
        ["tenant_id", "kind", "title"],
        unique=True,
        postgresql_where=sa.text("kind <> 'document'"),
    )
    op.create_table(
        "knowledge_passages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('portuguese', search_text)", persisted=True),
        ),
    )
    op.create_index("ix_knowledge_passages_tenant_id", "knowledge_passages", ["tenant_id"])
    op.create_index("ix_knowledge_passages_item_id", "knowledge_passages", ["item_id"])
    op.create_index(
        "ix_knowledge_passages_search_vector",
        "knowledge_passages",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_passages_search_vector", table_name="knowledge_passages")
    op.drop_index("ix_knowledge_passages_item_id", table_name="knowledge_passages")
    op.drop_index("ix_knowledge_passages_tenant_id", table_name="knowledge_passages")
    op.drop_table("knowledge_passages")
    op.drop_index("uq_knowledge_items_tenant_kind_title", table_name="knowledge_items")
    op.drop_index("ix_knowledge_items_tenant_id", table_name="knowledge_items")
    op.drop_table("knowledge_items")
    bind = op.get_bind()
    postgresql.ENUM(name="knowledge_item_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="knowledge_item_kind").drop(bind, checkfirst=True)
