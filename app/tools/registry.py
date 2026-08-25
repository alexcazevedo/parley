"""Merges tool executors into the single view the agent runtime consumes.

Ordering is the routing rule: the first executor whose `can_handle` returns
True owns the name, and `NoopExecutor` is always the terminal fallback. Adding
a source is one line at construction time:

    ToolRegistry([BuiltinToolExecutor(), HttpToolExecutor()])

`ToolRegistry()` with no argument builds `default_executors()` — that is what
the worker constructs, so a real run gets the customer's HTTP tools. Pass an
explicit list (including `[]`) to opt out; tests do.
"""

from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.models.agent import Agent
from app.tools.base import NoopExecutor, ToolContext, ToolExecutor, ToolResult
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor

logger = structlog.get_logger(__name__)


def default_executors() -> list[ToolExecutor]:
    """What a production run gets, in priority order.

    `HttpToolExecutor.can_handle` claims every name, so it stays last:
    `BuiltinExecutor` (`remember`, and `handoff_to_human` from ticket 08) is
    *prepended*, not appended, or it would never be reached.
    """
    return [BuiltinExecutor(), HttpToolExecutor()]


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
        for executor in self._executors:
            if executor.can_handle(name):
                return await executor.execute(name, arguments, ctx)
        return await self._fallback.execute(name, arguments, ctx)
