"""Executor for the customer's own HTTP tools (`Tool` rows).

This is the harness's core promise: the customer writes an HTTP endpoint, the
harness handles schema validation, credentials, SSRF, timeouts, recording and
error shaping. Nothing here ever raises at the runtime — every failure comes
back as `ToolResult(ok=False, error=...)` so the model can read it, apologise
or retry, and the turn keeps going.
"""

import json
import time
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.http_client import shared_client
from app.http_retry import RETRY_STATUSES, wait_before_retry
from app.llm.base import ToolSpec
from app.models.agent import Agent
from app.models.tool import Tool
from app.security import secrets as secrets_crypto
from app.tools import ledger
from app.tools.base import UNKNOWN_TOOL_ERROR, ToolContext, ToolResult
from app.tools.ssrf import SSRFError, assert_safe_url
from app.tools.validation import validate_arguments

logger = structlog.get_logger(__name__)

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

    async def repeat_safe(self, name: str, ctx: ToolContext) -> bool:
        """May a call whose earlier attempt has an unknown outcome run again?

        Only a GET: it reads, so repeating it cannot book, charge or notify
        twice. Every other method, and a tool that no longer exists, is not.
        """
        tool = await self._load_tool(ctx, name)
        return tool is not None and (tool.method or "POST").upper() == "GET"

    async def execute(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        arguments = arguments or {}
        tool = await self._load_tool(ctx, name)
        if tool is None:
            # No ToolExecution row: nothing was executed, and hallucinated tool
            # names would otherwise flood the table.
            return ToolResult.failure(f"{UNKNOWN_TOOL_ERROR}: {name}")

        # On record (committed, `STARTED`) before anything runs; this also
        # leaves no transaction open across the HTTP call below.
        execution = await ledger.begin(ctx, tool_name=name, arguments=arguments, tool_id=tool.id)
        started = time.perf_counter()
        invalid = validate_arguments(tool.input_schema, arguments)
        if invalid is not None:
            result, outcome_unknown = ToolResult.failure(invalid), False
        else:
            result, outcome_unknown = await self._run(tool, name, arguments)

        latency_ms = int((time.perf_counter() - started) * 1000)
        meta = {"latency_ms": latency_ms} | result.meta
        if result.ok:
            result = ToolResult.success(result.data, **meta)
        else:
            result = ToolResult.failure(result.error or "tool execution failed", **meta)
        if outcome_unknown:
            # The request may have reached the endpoint: the row stays
            # `STARTED`, so a re-run treats the call as unknown outcome
            # (repeated only for a GET) instead of reusing this error.
            await ledger.mark_unknown(
                ctx,
                execution,
                error=result.error or "tool execution failed",
                latency_ms=latency_ms,
            )
        else:
            await ledger.finish(ctx, execution, result, latency_ms=latency_ms)
        logger.info(
            "tools.http.executed",
            tool=name,
            ok=result.ok,
            latency_ms=latency_ms,
            status_code=result.meta.get("status_code"),
            outcome_unknown=outcome_unknown,
        )
        return result

    async def _run(
        self, tool: Tool, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolResult, bool]:
        """The HTTP call, every failure shaped into a `ToolResult`.

        Also returns whether the outcome is unknown: True when the call failed
        after its request may have reached the endpoint (a timeout other than
        `ConnectTimeout`, an `httpx.HTTPError` other than `ConnectError`, an
        unexpected exception). A blocked endpoint, a connect error or timeout
        (the request never left) and any HTTP response are known outcomes.

        Only `Exception`s are caught: a cancellation propagates and leaves the
        ledger row `STARTED`.
        """
        try:
            return await self._call(tool, arguments), False
        except SSRFError as exc:
            return ToolResult.failure(f"blocked endpoint: {exc}"), False
        except httpx.TimeoutException as exc:
            failure = ToolResult.failure(f"tool timed out after {tool.timeout_seconds}s")
            return failure, not isinstance(exc, httpx.ConnectTimeout)
        except httpx.HTTPError as exc:
            failure = ToolResult.failure(f"request failed: {type(exc).__name__}")
            return failure, not isinstance(exc, httpx.ConnectError)
        except Exception:
            # Never let a customer endpoint break the turn; the detail goes to
            # the log, not to the model.
            logger.exception("tools.http.unexpected_error", tool=name)
            return ToolResult.failure("tool execution failed"), True

    async def _call(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        settings = get_settings()
        cap = settings.tool_max_response_bytes
        method = (tool.method or "POST").upper()
        # Only a GET is repeated on 429/5xx; anything else is sent exactly once.
        attempts = settings.http_retry_attempts if method == "GET" else 1
        # `identity`: a compressed body could expand far past the cap in memory.
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        } | self._auth_headers(tool)
        params = None if method in BODY_METHODS else arguments
        body = arguments if method in BODY_METHODS else None

        attempt = 1
        while True:
            # Before every try, retries included.
            await assert_safe_url(tool.endpoint)
            # The shared client never follows redirects: they are the cheap way
            # around the SSRF check (see ssrf.py).
            async with shared_client().stream(
                method,
                tool.endpoint,
                params=params,
                json=body,
                headers=headers,
                timeout=tool.timeout_seconds,
            ) as response:
                status_code = response.status_code
                retry_after = response.headers.get("retry-after")
                if status_code not in RETRY_STATUSES or attempt >= attempts:
                    encoding = response.headers.get("content-encoding", "identity")
                    if encoding.strip().lower() not in ("", "identity"):
                        return ToolResult.failure(
                            "unsupported response encoding", status_code=status_code
                        )
                    raw, truncated = await _read_capped(response, cap)
                    is_success = response.is_success
                    break
            # Body unread: leaving the stream releases the connection.
            await wait_before_retry(status_code, attempt, retry_after, settings)
            attempt += 1

        meta: dict[str, Any] = {"status_code": status_code}
        if truncated:
            meta["truncated"] = True
            logger.warning("tools.http.response_truncated", tool=tool.name, cap=cap)
        if is_success:
            return ToolResult.success(self._parse(raw, truncated), **meta)
        detail = raw.decode("utf-8", errors="replace")[:MAX_ERROR_BODY_CHARS]
        return ToolResult.failure(f"HTTP {status_code}: {detail}".strip(), **meta)

    def _parse(self, raw: bytes, truncated: bool) -> Any:
        if truncated:
            # Never parse cut JSON.
            return {"text": raw.decode("utf-8", errors="replace"), "truncated": True}
        try:
            return json.loads(raw)
        except ValueError:
            return {"text": raw.decode("utf-8", errors="replace")}

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


async def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most `cap` bytes; stop reading once more than `cap` arrived.

    `aiter_raw` (not `aiter_bytes`): the cap applies to what is held in memory,
    so nothing may be decompressed on the way.
    """
    buf = bytearray()
    async for chunk in response.aiter_raw():
        buf.extend(chunk)
        if len(buf) > cap:
            return bytes(buf[:cap]), True
    return bytes(buf), False
