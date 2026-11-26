"""ModelJobCall model: one call of an auxiliary model job and the model that served it.

Written by `app.llm.jobs.record_job_call` after the call, in its own short
transaction. `agent_run_id` is set only for a job that runs inside an agent
run (the boundary check); it is a plain column, not a foreign key, so a
record never depends on the run row being committed first.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class ModelJobCall(Base, TenantScoped):
    __tablename__ = "model_job_calls"
    __table_args__ = (
        Index(
            "ix_model_job_calls_tenant_conversation_created",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # `app.llm.jobs.Job` value.
    job: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    # ok | error | timeout | unusable
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    handed_over: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
