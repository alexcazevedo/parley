"""Handoff model: a request/record of a conversation moving to a human operator."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class HandoffRequestedBy(enum.StrEnum):
    AGENT = "agent"
    USER = "user"
    OPERATOR = "operator"
    SYSTEM = "system"


class Handoff(Base, TenantScoped):
    __tablename__ = "handoffs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[HandoffRequestedBy] = mapped_column(
        pg_enum(HandoffRequestedBy, "handoff_requested_by"), nullable=False
    )
    # Best-effort LLM-written operator brief (who/what/why/tone). Nullable: the
    # escalation itself must never be blocked by a failed/slow summary — see
    # app/handoff/summarizer.py. Migration 0005.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
