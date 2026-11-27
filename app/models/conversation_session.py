"""ConversationSession model: one run of a conversation's messages with no long silence.

Written by `app/conversations/sessions.py` (migration 0031). A conversation has
at most one `open` row; an ended row (`pending`, then `done` or `failed` once
its recap is written) keeps the session's `seq` range and the running summary
it had when it ended. A conversation with no row yet is one open session
covering all its messages; its row is created on first need.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class SessionStatus(enum.StrEnum):
    OPEN = "open"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class SessionEndedBy(enum.StrEnum):
    SILENCE = "silence"
    CLOSED = "closed"


class ConversationSession(Base, TenantScoped):
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index(
            "uq_conversation_sessions_open",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_conversation_sessions_conversation_id", "conversation_id"),
        Index("ix_conversation_sessions_contact_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    first_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `SessionEndedBy` value.
    ended_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # `SessionStatus` value.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SessionStatus.OPEN.value
    )
    summary_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    recap_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
