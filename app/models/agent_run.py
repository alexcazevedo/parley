"""AgentRun model: one execution of the agent loop for a conversation turn."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    # Reply check (app/agents/boundaries.py): set only when the reply was
    # blocked. `blocked_text` is the model's original reply, never sent;
    # `output_text` then holds the refusal that went out instead.
    boundary_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The provider and model that answered (null on rows before they were recorded).
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # Ids (strings) of the inbound messages this run answers.
    input_message_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
