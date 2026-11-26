"""Discovery: keep parley's copy of an MCP server's tools current.

`sync_server` loads the server, ends the transaction, lists the server's tools
(no transaction is held across the call), then in one transaction, with the
server row locked, upserts the tools, deletes those gone and sets `status`,
`error`, `skipped` and `synced_at`. `allowed_tools`, when set, limits what is
imported. A tool whose exposed name (`<server>__<tool>`) does not match
`^[A-Za-z0-9_-]{1,64}$` is not imported and is listed in `skipped`.

A failure keeps the last known tools and sets `status=error` with the reason.
`synced_at` is the time of the last attempt either way, so a failing server
is tried again after `MCP_SYNC_INTERVAL_SECONDS`, not on every pass.

`sync_due` is one pass of the worker's `mcp_sync_loop` (`app/workers/main.py`).
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.mcp import client as mcp_client
from app.models.mcp_server import McpServer, McpServerStatus
from app.models.mcp_tool import McpTool

logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Servers one pass of the worker loop syncs at most.
MAX_SERVERS_PER_PASS = 20


def _importable(
    server: McpServer, found: list[mcp_client.DiscoveredTool]
) -> tuple[list[mcp_client.DiscoveredTool], list[str]]:
    allowed = set(server.allowed_tools) if server.allowed_tools is not None else None
    kept: dict[str, mcp_client.DiscoveredTool] = {}
    skipped: list[str] = []
    for tool in found:
        if allowed is not None and tool.name not in allowed:
            continue
        if not mcp_client.EXPOSED_NAME_RE.match(mcp_client.exposed_name(server.name, tool.name)):
            skipped.append(tool.name)
            continue
        kept[tool.name] = tool  # a name listed twice: the last one wins
    return list(kept.values()), sorted(set(skipped))


async def sync_server(
    session_factory: SessionFactory, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Discover `server_id`'s tools and store them; a missing server is ignored."""
    async with session_factory() as session:
        server = await session.scalar(
            select(McpServer).where(McpServer.tenant_id == tenant_id, McpServer.id == server_id)
        )
        if server is None:
            return
        await end_transaction(session)

        reason: str | None = None
        found: list[mcp_client.DiscoveredTool] = []
        try:
            found = await mcp_client.list_tools(server)
        except mcp_client.McpError as exc:
            reason = exc.reason

        locked = await session.scalar(
            select(McpServer)
            .where(McpServer.tenant_id == tenant_id, McpServer.id == server_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:  # deleted meanwhile
            await session.rollback()
            return
        locked.synced_at = datetime.now(UTC)
        if reason is not None:
            locked.status = McpServerStatus.ERROR
            locked.error = reason
            await session.commit()
            logger.warning("mcp.sync_failed", server_id=str(server_id), reason=reason)
            return

        tools, skipped = _importable(locked, found)
        for tool in tools:
            values = {
                "description": tool.description,
                "input_schema": tool.input_schema,
                "read_only": tool.read_only,
            }
            await session.execute(
                insert(McpTool)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    server_id=server_id,
                    name=tool.name,
                    **values,
                )
                .on_conflict_do_update(constraint="uq_mcp_tools_server_id_name", set_=values)
            )
        gone = delete(McpTool).where(
            McpTool.tenant_id == tenant_id, McpTool.server_id == server_id
        )
        if tools:
            gone = gone.where(McpTool.name.not_in([t.name for t in tools]))
        await session.execute(gone)
        locked.status = McpServerStatus.OK
        locked.error = None
        locked.skipped = skipped
        await session.commit()
        logger.info(
            "mcp.synced", server_id=str(server_id), tools=len(tools), skipped=len(skipped)
        )


async def sync_due(session_factory: SessionFactory) -> int:
    """Sync the servers (of every tenant) not synced for
    `MCP_SYNC_INTERVAL_SECONDS`; returns how many were synced."""
    interval = timedelta(seconds=get_settings().mcp_sync_interval_seconds)
    cutoff = datetime.now(UTC) - interval
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(McpServer.tenant_id, McpServer.id)
                .where(or_(McpServer.synced_at.is_(None), McpServer.synced_at <= cutoff))
                .order_by(McpServer.synced_at.asc().nulls_first(), McpServer.id)
                .limit(MAX_SERVERS_PER_PASS)
            )
        ).all()
        await end_transaction(session)
    count = 0
    for tenant_id, server_id in rows:
        try:
            await sync_server(session_factory, tenant_id, server_id)
        except Exception:
            logger.exception("mcp.sync_error", server_id=str(server_id))
            continue
        count += 1
    return count
