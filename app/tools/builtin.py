"""Harness-provided tools every agent gets for free, independent of the
tenant's own HTTP tools: `remember` (this ticket) and `handoff_to_human`
(ticket 08).

Registration is table-driven so ticket 08 only has to:

  1. add `(HANDOFF_SPEC, "handoff_enabled")` to `BUILTIN_TOOLS`
  2. add `"handoff_to_human": _handle_handoff` to `_HANDLERS`

No other change (registry ordering, `ToolExecutor` wiring) is required —
`BuiltinExecutor` itself is generic over both tables.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.memory import MemoryType
from app.tools.base import ToolContext, ToolResult
from app.tools.validation import validate_arguments

REMEMBER_SPEC = ToolSpec(
    name="remember",
    description=(
        "Store a durable fact, preference or issue about the current contact "
        "so future conversations can recall it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [t.value for t in MemoryType]},
            "content": {"type": "string", "minLength": 1},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["type", "content"],
        "additionalProperties": False,
    },
)


async def _handle_remember(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    invalid = validate_arguments(REMEMBER_SPEC.parameters, arguments)
    if invalid is not None:
        return ToolResult.failure(invalid)
    memory = await MemoryService(ctx.session).store(
        tenant_id=ctx.tenant_id,
        contact_id=ctx.contact_id,
        type=arguments["type"],
        content=arguments["content"],
        importance=arguments.get("importance", 3),
    )
    return ToolResult.success({"id": str(memory.id)})


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]

# (spec, agent-flag) pairs: the flag gates both what `specs()` advertises to
# the model and what `execute()` will actually run. Ticket 08 appends its own
# pair here.
BUILTIN_TOOLS: list[tuple[ToolSpec, str]] = [
    (REMEMBER_SPEC, "memory_enabled"),
]

# Ticket 08 adds "handoff_to_human": _handle_handoff here.
_HANDLERS: dict[str, Handler] = {
    "remember": _handle_remember,
}

_FLAG_BY_NAME: dict[str, str] = {spec.name: flag for spec, flag in BUILTIN_TOOLS}


class BuiltinExecutor:
    """Harness-provided tools, individually gated per agent by a boolean flag
    on `Agent` (`memory_enabled`, `handoff_enabled`, ...).
    """

    def can_handle(self, name: str) -> bool:
        return name in _HANDLERS

    async def specs(self, agent: Agent, session: AsyncSession) -> list[ToolSpec]:
        return [spec for spec, flag in BUILTIN_TOOLS if getattr(agent, flag, True)]

    async def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = _HANDLERS.get(name)
        if handler is None:
            return ToolResult.failure(f"unknown tool: {name}")
        flag = _FLAG_BY_NAME.get(name)
        if flag is not None and not getattr(ctx.agent, flag, True):
            return ToolResult.failure(f"tool disabled for this agent: {name}")
        return await handler(arguments or {}, ctx)
