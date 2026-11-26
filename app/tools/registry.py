"""Merges tool executors into the single view the agent runtime consumes.

Ordering is the routing rule: the first executor whose `can_handle` returns
True (and whose optional `owns(name, ctx)` agrees) owns the name, and
`NoopExecutor` is always the terminal fallback. Adding a source is one line
at construction time:

    ToolRegistry([BuiltinToolExecutor(), HttpToolExecutor()])

`ToolRegistry()` with no argument builds `default_executors()` — that is what
the worker constructs, so a real run gets the customer's HTTP tools. Pass an
explicit list (including `[]`) to opt out; tests do.

Before routing a call, `execute` asks the ledger whether an earlier attempt of
the same turn already made it (PRODUCT.md E3.7, `ledger.find_prior`):

- finished (`OK` or `ERROR`): the saved result is returned as it is, the tool
  does not run (`tools.result_reused`);
- `STARTED` (outcome unknown): the call runs again only if the owning
  executor's `repeat_safe(name, ctx)` returns True; otherwise the model gets
  `ledger.OUTCOME_UNKNOWN_ERROR` and the operator a `tools.outcome_unknown`
  warning. Executors without `repeat_safe` count as not safe. A repeated
  call keeps the earlier row's `call_id` (`ToolContext.repeat_call_id`).

A reused or refused call writes no `ToolExecution` row.
"""

import dataclasses
from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.models.agent import Agent
from app.models.tool_execution import ToolExecutionStatus
from app.tools import ledger
from app.tools.base import NoopExecutor, ToolContext, ToolExecutor, ToolResult
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor
from app.tools.mcp import McpToolExecutor

logger = structlog.get_logger(__name__)


def default_executors() -> list[ToolExecutor]:
    """What a production run gets, in priority order.

    `HttpToolExecutor.can_handle` claims every name, so it stays last:
    `BuiltinExecutor` (`remember`, and `handoff_to_human`) is
    *prepended*, not appended, or it would never be reached.
    `McpToolExecutor` comes before it and claims only the MCP tools of the
    turn's agent (its `owns`), so an HTTP tool named `a__b` still reaches HTTP.
    """
    return [BuiltinExecutor(), McpToolExecutor(), HttpToolExecutor()]


class ToolRegistry:
    def __init__(self, executors: Sequence[ToolExecutor] | None = None) -> None:
        self._executors: list[ToolExecutor] = list(
            default_executors() if executors is None else executors
        )
        # Never part of `_executors`: it handles everything, so it must stay last.
        self._fallback = NoopExecutor()

    def register(self, executor: ToolExecutor) -> None:
        """Append a source. Earlier registrations win on name collisions."""
        self._executors.append(executor)

    async def specs(self, agent: Agent, session: AsyncSession) -> list[ToolSpec]:
        """Every tool `agent` may call, first-registration-wins on duplicates."""
        merged: dict[str, ToolSpec] = {}
        for executor in self._executors:
            for spec in await executor.specs(agent, session):
                if spec.name in merged:
                    logger.warning("tools.duplicate_spec", name=spec.name)
                    continue
                merged[spec.name] = spec
        return list(merged.values())

    async def execute(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        executor = await self._route(name, ctx)
        prior = await ledger.find_prior(ctx, name, arguments or {})
        if prior is None:
            return await executor.execute(name, arguments, ctx)
        if prior.status != ToolExecutionStatus.STARTED:
            logger.info(
                "tools.result_reused",
                tool=name,
                execution_id=str(prior.id),
                conversation_id=str(ctx.conversation_id),
                ok=prior.status == ToolExecutionStatus.OK,
            )
            return ledger.replay(prior)
        repeat_safe = getattr(executor, "repeat_safe", None)
        if repeat_safe is not None and await repeat_safe(name, ctx):
            # The repeat carries the earlier attempt's call id, so the endpoint
            # can tell it is the same call.
            repeat_ctx = dataclasses.replace(ctx, repeat_call_id=prior.call_id)
            return await executor.execute(name, arguments, repeat_ctx)
        logger.warning(
            "tools.outcome_unknown",
            tool=name,
            execution_id=str(prior.id),
            conversation_id=str(ctx.conversation_id),
        )
        return ToolResult.failure(ledger.OUTCOME_UNKNOWN_ERROR)

    async def _route(self, name: str, ctx: ToolContext) -> ToolExecutor:
        """The first executor whose `can_handle` is True and, if it has an
        `owns(name, ctx)` (a check that needs the turn), whose `owns` is True."""
        for executor in self._executors:
            if not executor.can_handle(name):
                continue
            owns = getattr(executor, "owns", None)
            if owns is not None and not await owns(name, ctx):
                continue
            return executor
        return self._fallback
