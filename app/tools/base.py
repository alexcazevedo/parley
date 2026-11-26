"""Tool execution contracts.

A `ToolExecutor` is one *source* of tools, not one tool: one executor is
backed by the tenant's HTTP `Tool` rows, another serves the built-ins
(`remember`, `handoff_to_human`). The registry merges them.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.models.agent import Agent
from app.tools.context import CallContext

UNKNOWN_TOOL_ERROR = "unknown tool"


@dataclass(slots=True)
class ToolContext:
    """Everything an executor may need about the turn it is running inside.

    Deliberately carries no credentials: an executor that needs tenant secrets
    loads and decrypts them itself from `session` (see app/security/secrets.py).
    """

    session: AsyncSession
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    agent: Agent
    agent_run_id: uuid.UUID
    # Ids of the inbound messages this turn answers. A tool call made by an
    # earlier attempt of the same batch is not made again (app/tools/ledger.py
    # `find_prior`); empty means no reuse at all.
    message_ids: tuple[uuid.UUID, ...] = ()
    # Who the calls are for (app/tools/context.py), loaded once per turn. Stored
    # on every `ToolExecution` row and sent to HTTP tools as headers.
    call: CallContext | None = None
    # Set by the registry when it repeats a call whose earlier attempt has an
    # unknown outcome: the repeat keeps that attempt's call id.
    repeat_call_id: uuid.UUID | None = None


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    # Free-form execution details (status code, duration, ...) for callers that
    # want to persist a ToolExecution row. Never sent to the model.
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, data: Any = None, **meta: Any) -> "ToolResult":
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=error, meta=meta)

    def to_content(self) -> str:
        """JSON body handed back to the model as the `role="tool"` message."""
        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({"ok": False, "error": "tool result is not serializable"})


@runtime_checkable
class ToolExecutor(Protocol):
    """One source of callable tools."""

    def can_handle(self, name: str) -> bool:
        """True if `execute` should be routed this tool name."""
        ...

    async def specs(self, agent: Agent, session: AsyncSession) -> list[ToolSpec]:
        """Tools this executor offers to `agent` (already tenant-scoped)."""
        ...

    async def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...


class NoopExecutor:
    """Terminal fallback: advertises nothing, fails every call.

    Keeps the runtime honest when a model hallucinates a tool name — the error
    goes back into the transcript instead of raising.
    """

    def can_handle(self, name: str) -> bool:
        return True

    async def specs(self, agent: Agent, session: AsyncSession) -> list[ToolSpec]:
        return []

    async def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.failure(f"{UNKNOWN_TOOL_ERROR}: {name}")
