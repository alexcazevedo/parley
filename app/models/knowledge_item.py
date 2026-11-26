"""KnowledgeItem model: one entry, question-and-answer pair or document of a tenant."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class KnowledgeItemKind(enum.StrEnum):
    ENTRY = "entry"
    QA = "qa"
    DOCUMENT = "document"


class KnowledgeItemStatus(enum.StrEnum):
    # Set on create and on every edit of title or text; the worker
    # (app/knowledge/ingest.py) splits the item and sets `ready` or `failed`.
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeItem(Base, TenantScoped):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        # Documents may share a title, so a replacement can be uploaded under
        # the same name before the old one is deleted.
        Index(
            "uq_knowledge_items_tenant_kind_title",
            "tenant_id",
            "kind",
            "title",
            unique=True,
            postgresql_where=text("kind <> 'document'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[KnowledgeItemKind] = mapped_column(
        pg_enum(KnowledgeItemKind, "knowledge_item_kind"), nullable=False
    )
    # An entry's title, a pair's question, a document's name.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    # An entry's text, a pair's answer; null for a document.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[KnowledgeItemStatus] = mapped_column(
        pg_enum(KnowledgeItemStatus, "knowledge_item_status"),
        nullable=False,
        default=KnowledgeItemStatus.PROCESSING,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_key: Mapped[str | None] = mapped_column(String, nullable=True)
    source_mime: Mapped[str | None] = mapped_column(String, nullable=True)
    # Lease of the worker processing the item (app/knowledge/ingest.py).
    claimed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set explicitly on every edit (app/api/knowledge.py); the worker only
    # finishes an item whose `updated_at` it saw when it claimed it.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
