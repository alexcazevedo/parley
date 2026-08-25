"""AgentRun model: one execution of the agent loop for a conversation turn."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class AgentRunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    # Barge-in: a newer inbound message arrived and the turn dropped its unsent
    # reply at a checkpoint (app/conversations/turn.py). Observable, not an
    # error — the batch is re-answered by the next turn.
    SUPERSEDED = "superseded"


class AgentRun(Base, TenantScoped):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        pg_enum(AgentRunStatus, "agent_run_status"),
        nullable=False,
        default=AgentRunStatus.RUNNING,
    )
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
