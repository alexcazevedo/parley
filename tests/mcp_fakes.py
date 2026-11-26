"""In-process MCP servers for tests; no test opens a real MCP connection.

`fake_server()` is a shop with `search_product` (read-only) and `add_to_quote`;
every call is appended to `server.calls` as `{"name", "arguments", "meta"}`.

The `mcp_connect` fixture replaces `app.mcp.client.connect` and returns a dict
from URL to what that URL reaches: an `MCPServer` (connected in-process) or an
exception (raised when connecting, e.g. `McpError("could not reach the server")`).
An unknown URL raises `McpError("could not reach the server")`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

import app.mcp.client as mcp_client
from app.models.mcp_server import McpServer

SHOP_URL = "https://shop.example.com/mcp"


def _meta(ctx: Context) -> dict[str, Any]:
    meta = getattr(ctx.request_context, "meta", None)
    if meta is None:
        return {}
    if hasattr(meta, "model_dump"):
        return dict(meta.model_dump(by_alias=True, exclude_none=True))
    return dict(meta)


def fake_server() -> MCPServer:
    server = MCPServer("shop")
    calls: list[dict[str, Any]] = []
    server.calls = calls  # type: ignore[attr-defined]

    @server.tool(
        description="Search the catalog by text.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def search_product(query: str, ctx: Context) -> str:
        calls.append({"name": "search_product", "arguments": {"query": query}, "meta": _meta(ctx)})
        return f"1 product found for {query}: cartolina azul, R$ 2,50"

    @server.tool(description="Add a product to the customer's quote.")
    async def add_to_quote(product_id: str, quantity: int, ctx: Context) -> str:
        calls.append(
            {
                "name": "add_to_quote",
                "arguments": {"product_id": product_id, "quantity": quantity},
                "meta": _meta(ctx),
            }
        )
        return f"added {quantity} x {product_id}"

    return server


@pytest.fixture
def mcp_connect(monkeypatch) -> dict[str, Any]:
    routes: dict[str, Any] = {}

    @asynccontextmanager
    async def connect(server: McpServer) -> AsyncIterator[Client]:
        target = routes.get(server.url)
        if target is None:
            raise mcp_client.McpError("could not reach the server", sent=False)
        if isinstance(target, BaseException):
            raise target
        async with Client(target) as client:
            yield client

    monkeypatch.setattr(mcp_client, "connect", connect)
    return routes
