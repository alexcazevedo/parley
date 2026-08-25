"""Conversation model: the stateful thread between a contact and an agent."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class ConversationState(enum.StrEnum):
    ACTIVE_AI = "ACTIVE_AI"
    WAITING = "WAITING"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    CLOSED = "CLOSED"


class Conversation(Base, TenantScoped):
    __tablename__ = "conversations"

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
        SAEnum(ConversationState, name="conversation_state", native_enum=True),
        nullable=False,
        default=ConversationState.ACTIVE_AI,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Count of messages already folded into `summary` (see migration 0003 and
    # app/conversations/summary.py). NULL == nothing summarized yet.
    summary_cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
