"""Conversation model: the stateful thread between a contact and an agent."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class ConversationState(enum.StrEnum):
    ACTIVE_AI = "ACTIVE_AI"
    WAITING = "WAITING"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    CLOSED = "CLOSED"


class Conversation(Base, TenantScoped):
    __tablename__ = "conversations"
    # At most one open (not `CLOSED`) conversation per contact and channel
    # (migration 0012); `app/conversations/resolver.py` inserts against it.
    __table_args__ = (
        Index(
            "uq_conversations_open_contact_channel",
            "tenant_id",
            "contact_id",
            "channel_id",
            unique=True,
            postgresql_where=text("state != 'CLOSED'"),
        ),
        # The waiting lines (migration 0015, `GET /lines`).
        Index(
            "ix_conversations_waiting",
            "tenant_id",
            "waiting_since",
            postgresql_where=text("state = 'HUMAN_HANDOFF'"),
        ),
        # The operator inbox list (migration 0016).
        Index(
            "ix_conversations_tenant_state_last_message_at",
            "tenant_id",
            "state",
            "last_message_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[ConversationState] = mapped_column(
        pg_enum(ConversationState, "conversation_state"),
        nullable=False,
        default=ConversationState.ACTIVE_AI,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `summary` covers the messages with `seq <= summary_last_seq` (migration
    # 0013, app/conversations/summary.py). NULL == nothing summarized yet.
    summary_last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Subject chosen from the tenant's categories (migration 0014); NULL when
    # nothing fits or the category was deleted. `category_locked` is true once
    # a human chose it: the classifier then leaves it alone.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # When the current handoff began (migration 0015). Written only by
    # `HandoffService` (database clock), so a category move keeps the place in
    # line. `claimed_by`/`claimed_at` are written by the claim endpoints and
    # cleared whenever the conversation enters or leaves `HUMAN_HANDOFF`.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
