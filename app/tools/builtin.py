"""Harness-provided tools every agent gets for free, independent of the
tenant's own HTTP tools: `remember` and `handoff_to_human`.

Registration is table-driven: each tool is a `(ToolSpec, agent-flag)` pair in
`BUILTIN_TOOLS` plus a handler in `_HANDLERS`. No other change (registry
ordering, `ToolExecutor` wiring) is required — `BuiltinExecutor` itself is
generic over both tables.

`handoff_to_human`'s `ToolResult.meta` carries `terminal=True` and
`final_text=<agent.handoff_message>`: `AgentRuntime._loop` (app/agents/runtime.py)
treats any terminal tool result as "stop calling the model, send this text
instead" — the mechanism any terminal tool uses.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.handoff.service import IllegalTransitionError
from app.handoff.summarizer import handoff_with_summary
from app.llm.base import ToolSpec
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.handoff import HandoffRequestedBy
from app.models.memory import MemoryType
from app.tools import ledger
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


HANDOFF_SPEC = ToolSpec(
    name="handoff_to_human",
    description=(
        "Transfer this conversation to a human operator: use it when the user "
        "explicitly asks for a person, is upset, or the request is outside "
        "what you can help with. The AI stops replying until an operator "
        "resumes the conversation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
)


async def _handle_handoff(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    invalid = validate_arguments(HANDOFF_SPEC.parameters, arguments)
    if invalid is not None:
        return ToolResult.failure(invalid)
    conversation = await ctx.session.get(Conversation, ctx.conversation_id)
    if conversation is None:
        return ToolResult.failure("conversation not found")
    try:
        await handoff_with_summary(
            ctx.session, conversation, arguments["reason"], HandoffRequestedBy.AGENT
        )
    except IllegalTransitionError as exc:
        return ToolResult.failure(str(exc))
    # `terminal`/`final_text` tell `AgentRuntime._loop` to stop calling the
    # model and send `agent.handoff_message` instead — see app/agents/runtime.py.
    return ToolResult.success(
        {"ok": True}, terminal=True, final_text=ctx.agent.handoff_message
    )


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]

# (spec, agent-flag) pairs: the flag gates both what `specs()` advertises to
# the model and what `execute()` will actually run.
BUILTIN_TOOLS: list[tuple[ToolSpec, str]] = [
    (REMEMBER_SPEC, "memory_enabled"),
    (HANDOFF_SPEC, "handoff_enabled"),
]

_HANDLERS: dict[str, Handler] = {
    "remember": _handle_remember,
    "handoff_to_human": _handle_handoff,
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

    async def repeat_safe(self, name: str, ctx: ToolContext) -> bool:
        """Always True: a built-in's effect is committed together with its
        result (`ledger.finish`), so a row left `STARTED` means nothing happened.
        """
        return True

    async def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = _HANDLERS.get(name)
        if handler is None:
            return ToolResult.failure(f"unknown tool: {name}")
        flag = _FLAG_BY_NAME.get(name)
        if flag is not None and not getattr(ctx.agent, flag, True):
            return ToolResult.failure(f"tool disabled for this agent: {name}")
        arguments = arguments or {}
        # Committed `STARTED` before the handler runs; `finish` commits the
        # handler's own writes together with the result, so a row left
        # `STARTED` means the call had no effect. A handler that raises is not
        # caught: the turn's rollback drops its writes and the row stays
        # `STARTED`.
        execution = await ledger.begin(ctx, tool_name=name, arguments=arguments)
        started = time.perf_counter()
        result = await handler(arguments, ctx)
        latency_ms = int((time.perf_counter() - started) * 1000)
        await ledger.finish(ctx, execution, result, latency_ms=latency_ms)
        return result
