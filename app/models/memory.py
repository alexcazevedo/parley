"""Memory model: a durable fact/preference the agent recorded about a contact."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class MemoryType(enum.StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    RELATIONSHIP = "relationship"
    PREVIOUS_ISSUE = "previous_issue"
    SUMMARY = "summary"
    CUSTOM = "custom"


class Memory(Base, TenantScoped):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("importance >= 1 AND importance <= 5", name="ck_memories_importance_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[MemoryType] = mapped_column(
        SAEnum(MemoryType, name="memory_type", native_enum=True), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mapped to the DB column "metadata"; the Python attribute is `meta` because
    # `metadata` is reserved by SQLAlchemy's declarative Base.
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
