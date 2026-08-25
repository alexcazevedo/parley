"""Message model: one inbound or outbound message on a conversation.

`channel_id` is denormalized from the conversation onto the message itself
(rather than only reachable via a join) because idempotency on inbound
webhook deliveries is keyed on `(tenant_id, channel_id, external_message_id)`.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class MessageDirection(enum.StrEnum):
    IN = "in"
    OUT = "out"


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    HUMAN_OPERATOR = "human_operator"
    TOOL = "tool"
    SYSTEM = "system"


class Message(Base, TenantScoped):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_tenant_id_channel_id_external_message_id",
            "tenant_id",
            "channel_id",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        pg_enum(MessageDirection, "message_direction"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        pg_enum(MessageRole, "message_role"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="text")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
