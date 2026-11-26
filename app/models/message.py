"""Message model: one inbound or outbound message on a conversation.

`channel_id` is denormalized from the conversation onto the message itself
(rather than only reachable via a join) because idempotency on inbound
webhook deliveries is keyed on `(tenant_id, channel_id, external_message_id)`.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Sequence,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum

# The order of a conversation (migration 0013). Taken from the database at
# insert, never from a clock: `created_at` is stamped by the database for
# inbound rows and by the app for the others, so it is only for display.
# Declared on the metadata so `create_all` builds it before `messages`.
MESSAGES_SEQ = Sequence("messages_seq", metadata=Base.metadata)


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
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
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
    media_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Position in the conversation; every read of a conversation orders by it.
    # A server default (not a client-side sequence), fetched back on insert.
    seq: Mapped[int] = mapped_column(
        BigInteger, server_default=MESSAGES_SEQ.next_value(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
