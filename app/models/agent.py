"""Agent model: an LLM persona bound to instructions, model config, and tools."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class Agent(Base, TenantScoped):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="openai_compatible", server_default="openai_compatible"
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    # No default specified by the ticket; 0.7 is a reasonable OpenAI-compatible default.
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    max_tool_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # Not given explicit defaults by the ticket; both default on since the harness's
    # core value prop (memory + handoff) should be opt-out, not opt-in.
    memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    handoff_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tool_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    context_message_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    summary_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
