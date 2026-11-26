"""ToolExecution model: one call an agent run made to a Tool."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class ToolExecutionStatus(enum.StrEnum):
    # Committed before the tool runs (app/tools/ledger.py). A row still
    # `started` later means the call's outcome is unknown: it was cut off
    # (cancellation, worker death) before it returned, or it failed in a way
    # that may have reached the endpoint (a timeout other than a connect
    # timeout; `error`/`latency_ms` are then filled in by `ledger.mark_unknown`).
    STARTED = "started"
    OK = "ok"
    ERROR = "error"


class ToolExecution(Base, TenantScoped):
    __tablename__ = "tool_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable + SET NULL: the Tool row may be deleted later, but the execution
    # record (and denormalized `tool_name`) must survive for observability.
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tools.id", ondelete="SET NULL"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ToolExecutionStatus] = mapped_column(
        pg_enum(ToolExecutionStatus, "tool_execution_status"), nullable=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The exact string the model received (`ToolResult.to_content()`); null
    # while `started`.
    model_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A terminal result's `final_text` (`handoff_to_human`): the reply that
    # ended the loop, restored by `ledger.replay` so a reused call ends it too.
    terminal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # One per call (app/tools/ledger.py `begin`); a repeat of an unknown-outcome
    # call keeps the earlier row's. Sent to HTTP tools as `X-Parley-Call-Id`.
    # Null on rows written before migration 0025.
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Who the call was for (`CallContext.as_record()`); null on old rows.
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
