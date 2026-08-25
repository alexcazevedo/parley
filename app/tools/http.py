"""Executor for the customer's own HTTP tools (`Tool` rows).

This is the harness's core promise: the customer writes an HTTP endpoint, the
harness handles schema validation, credentials, SSRF, timeouts, recording and
error shaping. Nothing here ever raises at the runtime — every failure comes
back as `ToolResult(ok=False, error=...)` so the model can read it, apologise
or retry, and the turn keeps going.
"""

import json
import time
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.models.agent import Agent
from app.models.tool import Tool
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.security import secrets as secrets_crypto
from app.tools.base import UNKNOWN_TOOL_ERROR, ToolContext, ToolResult
from app.tools.ssrf import SSRFError, assert_safe_url
from app.tools.validation import validate_arguments

logger = structlog.get_logger(__name__)

# Non-JSON bodies are handed to the model as {"text": ...}; anything past this
# is dead weight in the prompt.
MAX_TEXT_BYTES = 8 * 1024
# How much of an error body we quote back. Small on purpose: it goes into the
# transcript and may contain the customer's internals.
MAX_ERROR_BODY_CHARS = 500
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class HttpToolExecutor:
    """Runs the tenant's `Tool` rows.

    `can_handle` returns True for *every* name: whether a tool exists is a
    per-tenant database question this method cannot answer (it has no
    `ToolContext`). Consequence for the registry: this executor claims
    everything, so it must be registered last — see `app/tools/registry.py`.
    Unknown names produce the same `unknown tool` result `NoopExecutor` would.
    """

    def can_handle(self, name: str) -> bool:
        return True

    async def specs(self, agent: Agent, session: AsyncSession) -> list[ToolSpec]:
        tools = await self._load_tools(session, agent)
        return [
            ToolSpec(
                name=tool.name,
                description=tool.description or tool.name,
                parameters=tool.input_schema or {"type": "object", "properties": {}},
            )
            for tool in tools
        ]

    async def execute(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        arguments = arguments or {}
        tool = await self._load_tool(ctx, name)
        if tool is None:
            # No ToolExecution row: nothing was executed, and hallucinated tool
            # names would otherwise flood the table.
            return ToolResult.failure(f"{UNKNOWN_TOOL_ERROR}: {name}")

        started = time.perf_counter()
        invalid = validate_arguments(tool.input_schema, arguments)
        if invalid is not None:
            return await self._record(ctx, tool, name, arguments, started, error=invalid)

        try:
            result = await self._call(tool, arguments)
        except SSRFError as exc:
            result = ToolResult.failure(f"blocked endpoint: {exc}")
        except httpx.TimeoutException:
            result = ToolResult.failure(f"tool timed out after {tool.timeout_seconds}s")
        except httpx.HTTPError as exc:
            result = ToolResult.failure(f"request failed: {type(exc).__name__}")
        except Exception:
            # Never let a customer endpoint break the turn; the detail goes to
            # the log, not to the model.
            logger.exception("tools.http.unexpected_error", tool=name)
            result = ToolResult.failure("tool execution failed")

        return await self._record(
            ctx,
            tool,
            name,
            arguments,
            started,
            error=result.error,
            data=result.data,
            meta=result.meta,
        )

    async def _call(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        await assert_safe_url(tool.endpoint)
        method = (tool.method or "POST").upper()
        headers = {"Accept": "application/json"} | self._auth_headers(tool)
        params = None if method in BODY_METHODS else arguments
        body = arguments if method in BODY_METHODS else None

        async with httpx.AsyncClient(
            timeout=tool.timeout_seconds,
            # Redirects are the cheap way around the SSRF check (see ssrf.py).
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method, tool.endpoint, params=params, json=body, headers=headers
            )

        payload = self._parse(response)
        meta = {"status_code": response.status_code}
        if response.is_success:
            return ToolResult.success(payload, **meta)
        detail = response.text[:MAX_ERROR_BODY_CHARS]
        return ToolResult.failure(f"HTTP {response.status_code}: {detail}".strip(), **meta)

    def _parse(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            text = response.content[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
            return {"text": text}

    def _auth_headers(self, tool: Tool) -> dict[str, str]:
        """Decrypt `auth_config` at call time. The token is never logged."""
        if not tool.auth_config_encrypted:
            return {}
        try:
            config = json.loads(secrets_crypto.decrypt(tool.auth_config_encrypted))
        except Exception:
            logger.warning("tools.http.auth_config_unreadable", tool=tool.name)
            return {}
        if not isinstance(config, dict):
            return {}

        auth_type = str(config.get("type") or "none").lower()
        token = config.get("token") or ""
        if auth_type == "bearer" and token:
            return {"Authorization": f"Bearer {token}"}
        if auth_type == "header" and token:
            return {str(config.get("header_name") or "Authorization"): str(token)}
        return {}

    async def _load_tool(self, ctx: ToolContext, name: str) -> Tool | None:
        """The tool must exist for the tenant *and* be listed on the agent."""
        if name not in (ctx.agent.tool_names or []):
            return None
        result = await ctx.session.execute(
            select(Tool).where(Tool.tenant_id == ctx.tenant_id, Tool.name == name)
        )
        return result.scalar_one_or_none()

    async def _load_tools(self, session: AsyncSession, agent: Agent) -> list[Tool]:
        names = list(agent.tool_names or [])
        if not names:
            return []
        result = await session.execute(
            select(Tool).where(Tool.tenant_id == agent.tenant_id, Tool.name.in_(names))
        )
        return list(result.scalars().all())

    async def _record(
        self,
        ctx: ToolContext,
        tool: Tool,
        name: str,
        arguments: dict[str, Any],
        started: float,
        *,
        error: str | None = None,
        data: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Persist the `ToolExecution` and return what the model will see."""
        latency_ms = int((time.perf_counter() - started) * 1000)
        ok = error is None
        ctx.session.add(
            ToolExecution(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                agent_run_id=ctx.agent_run_id,
                tool_id=tool.id,
                tool_name=name,
                arguments=_jsonable(arguments) or {},
                result=_jsonable(data) if ok else None,
                status=ToolExecutionStatus.OK if ok else ToolExecutionStatus.ERROR,
                latency_ms=latency_ms,
                error=error,
            )
        )
        await ctx.session.flush()
        logger.info(
            "tools.http.executed",
            tool=name,
            ok=ok,
            latency_ms=latency_ms,
            status_code=(meta or {}).get("status_code"),
        )
        if ok:
            return ToolResult.success(data, latency_ms=latency_ms, **(meta or {}))
        return ToolResult.failure(
            error or "tool execution failed", latency_ms=latency_ms, **(meta or {})
        )


def _jsonable(value: Any) -> dict[str, Any] | None:
    """`ToolExecution.arguments/result` are JSONB objects; wrap non-objects."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"data": value}
