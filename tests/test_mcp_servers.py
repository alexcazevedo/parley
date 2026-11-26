"""`/mcp-servers`: registration with discovery, re-sync, allow-list, failures
(kept tools, OAuth reason), address checks, a write-only token, paging,
tenant scoping, and the worker's `sync_due`."""

import functools
import logging
import os
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx2
import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlalchemy import update

import app.mcp.client as mcp_client
import app.tools.ssrf as ssrf
from app.mcp.sync import sync_due
from app.models.mcp_server import McpServer
from tests.mcp_fakes import SHOP_URL, fake_server, mcp_connect  # noqa: F401

TOKEN = "s3cr3t-mcp-token-value"


async def _tenant(client, name: str = "Shop") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _register(client, headers, **body):
    payload = {"name": "shop", "url": SHOP_URL} | body
    return await client.post("/mcp-servers", json=payload, headers=headers)


def _tools(body: dict) -> dict[str, dict]:
    return {t["name"]: t for t in body["tools"]}


async def test_register_imports_tools_with_read_only(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)

    response = await _register(
        client, headers, auth_config={"type": "bearer", "token": TOKEN}, timeout_seconds=5
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id", "name", "url", "allowed_tools", "timeout_seconds", "status", "error",
        "skipped", "synced_at", "created_at", "tools",
    }
    assert body["status"] == "ok" and body["error"] is None and body["synced_at"]
    assert body["timeout_seconds"] == 5 and body["allowed_tools"] is None
    tools = _tools(body)
    assert set(tools) == {"search_product", "add_to_quote"}
    assert tools["search_product"]["read_only"] is True
    assert tools["add_to_quote"]["read_only"] is False
    assert tools["search_product"]["exposed_name"] == "shop__search_product"
    assert tools["search_product"]["description"] == "Search the catalog by text."
    assert "query" in tools["search_product"]["input_schema"]["properties"]
    assert TOKEN not in response.text

    got = await client.get(f"/mcp-servers/{body['id']}", headers=headers)
    assert got.status_code == 200 and got.json() == body


async def test_resync_adds_updates_and_removes(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    server_id = (await _register(client, headers)).json()["id"]

    changed = MCPServer("shop")

    @changed.tool(
        description="Search the catalog, new text.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def search_product(query: str, page: int = 1) -> str:
        return query

    @changed.tool(
        description="Price of one product.", annotations=ToolAnnotations(read_only_hint=True)
    )
    async def get_price(product_id: str) -> str:
        return product_id

    mcp_connect[SHOP_URL] = changed
    response = await client.post(f"/mcp-servers/{server_id}/sync", headers=headers)

    assert response.status_code == 200, response.text
    tools = _tools(response.json())
    assert set(tools) == {"search_product", "get_price"}
    assert tools["search_product"]["description"] == "Search the catalog, new text."
    assert "page" in tools["search_product"]["input_schema"]["properties"]
    assert tools["get_price"]["read_only"] is True


async def test_allowed_tools_limits_the_import(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)

    body = (await _register(client, headers, allowed_tools=["search_product", "nope"])).json()
    assert set(_tools(body)) == {"search_product"}
    assert body["allowed_tools"] == ["search_product", "nope"]

    widened = await client.patch(
        f"/mcp-servers/{body['id']}", json={"allowed_tools": None}, headers=headers
    )
    assert widened.status_code == 200, widened.text
    assert set(_tools(widened.json())) == {"search_product", "add_to_quote"}


async def test_invalid_exposed_name_is_skipped(client, mcp_connect):  # noqa: F811
    server = MCPServer("odd")

    @server.tool(name="get.price")
    async def dotted() -> str:
        return ""

    @server.tool(name="x" * 60)
    async def too_long() -> str:
        return ""

    @server.tool()
    async def fine() -> str:
        return ""

    mcp_connect[SHOP_URL] = server
    headers = await _tenant(client)
    body = (await _register(client, headers)).json()

    assert body["status"] == "ok"
    assert set(_tools(body)) == {"fine"}
    assert sorted(body["skipped"]) == sorted(["get.price", "x" * 60])


async def test_failing_server_keeps_its_tools(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    server_id = (await _register(client, headers)).json()["id"]

    mcp_connect[SHOP_URL] = mcp_client.McpError("could not reach the server", sent=False)
    response = await client.post(f"/mcp-servers/{server_id}/sync", headers=headers)

    body = response.json()
    assert body["status"] == "error"
    assert body["error"] == "could not reach the server"
    assert set(_tools(body)) == {"search_product", "add_to_quote"}

    mcp_connect[SHOP_URL] = RuntimeError("boom")
    body = (await client.post(f"/mcp-servers/{server_id}/sync", headers=headers)).json()
    assert body["status"] == "error" and "boom" in body["error"]

    mcp_connect[SHOP_URL] = fake_server()
    body = (await client.post(f"/mcp-servers/{server_id}/sync", headers=headers)).json()
    assert body["status"] == "ok" and body["error"] is None


def _mock_http(monkeypatch, handler) -> None:
    """Real `connect`, with the HTTP client on a mock transport (no socket)."""
    original = mcp_client._http_client
    monkeypatch.setattr(
        mcp_client,
        "_http_client",
        functools.partial(original, transport=httpx2.MockTransport(handler)),
    )


async def test_oauth_401_sets_the_oauth_reason_and_never_logs_the_token(
    client, monkeypatch, caplog
):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://x/.well-known"'}
        )

    _mock_http(monkeypatch, handler)
    headers = await _tenant(client)
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        response = await _register(
            client, headers, auth_config={"type": "bearer", "token": TOKEN}
        )

    body = response.json()
    assert response.status_code == 201
    assert body["status"] == "error"
    assert body["error"] == mcp_client.OAUTH_REASON
    assert body["tools"] == []
    assert seen and seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(logs)


async def test_refused_token_without_oauth(client, monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx2.Response(401))
    headers = await _tenant(client)
    body = (
        await _register(
            client, headers, auth_config={"type": "header", "header_name": "X-Key", "token": TOKEN}
        )
    ).json()
    assert body["status"] == "error"
    assert body["error"] == "the server refused the token (HTTP 401)"


async def test_private_address_refused_at_registration(client, mcp_connect):  # noqa: F811
    headers = await _tenant(client)
    for url in ("http://10.0.0.5/mcp", "http://localhost:8000/mcp", "ftp://shop.example.com/"):
        response = await _register(client, headers, url=url)
        assert response.status_code == 422, (url, response.text)
    assert (await client.get("/mcp-servers", headers=headers)).json() == []


async def test_private_address_refused_at_connection(client, monkeypatch):
    async def resolve(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    monkeypatch.setattr(ssrf, "_resolve", resolve)
    _mock_http(monkeypatch, lambda request: pytest_fail_request(request))
    headers = await _tenant(client)
    body = (await _register(client, headers, url="https://rebound.example.org/mcp")).json()
    assert body["status"] == "error"
    assert body["error"].startswith("address refused: blocked internal address")


def pytest_fail_request(request):  # pragma: no cover - must not be reached
    raise AssertionError(f"connected to {request.url}")


async def test_lists_page(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    for name in ("a", "b", "c"):
        assert (await _register(client, headers, name=name)).status_code == 201

    first = await client.get("/mcp-servers", params={"limit": 2}, headers=headers)
    assert [s["name"] for s in first.json()] == ["a", "b"]
    assert all(len(s["tools"]) == 2 for s in first.json())
    cursor = first.headers["X-Next-Cursor"]
    second = await client.get(
        "/mcp-servers", params={"limit": 2, "cursor": cursor}, headers=headers
    )
    assert [s["name"] for s in second.json()] == ["c"]
    assert "X-Next-Cursor" not in second.headers


async def test_name_rules_and_duplicates(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    for bad in ("Shop", "shop-1", "x" * 25, ""):
        assert (await _register(client, headers, name=bad)).status_code == 422, bad
    assert (await _register(client, headers, timeout_seconds=61)).status_code == 422
    assert (
        await _register(client, headers, auth_config={"type": "oauth", "token": "x"})
    ).status_code == 422
    assert (await _register(client, headers)).status_code == 201
    assert (await _register(client, headers)).status_code == 409


async def test_other_tenant_cannot_touch_the_server(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    a = await _tenant(client, "A")
    b = await _tenant(client, "B")
    server_id = (await _register(client, a)).json()["id"]

    assert (await client.get(f"/mcp-servers/{server_id}", headers=b)).status_code == 404
    assert (await client.get("/mcp-servers", headers=b)).json() == []
    patched = await client.patch(f"/mcp-servers/{server_id}", json={"name": "x"}, headers=b)
    assert patched.status_code == 404
    assert (await client.post(f"/mcp-servers/{server_id}/sync", headers=b)).status_code == 404
    assert (await client.delete(f"/mcp-servers/{server_id}", headers=b)).status_code == 404

    # B may use the same name.
    assert (await _register(client, b)).status_code == 201
    own = (await client.get(f"/mcp-servers/{server_id}", headers=a)).json()
    assert own["name"] == "shop" and len(own["tools"]) == 2

    assert (await client.delete(f"/mcp-servers/{server_id}", headers=a)).status_code == 204
    assert (await client.get(f"/mcp-servers/{server_id}", headers=a)).status_code == 404


async def test_patch_token_stays_write_only(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    server_id = (await _register(client, headers)).json()["id"]
    response = await client.patch(
        f"/mcp-servers/{server_id}",
        json={"auth_config": {"type": "bearer", "token": TOKEN}, "name": "renamed"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text
    assert response.json()["tools"][0]["exposed_name"].startswith("renamed__")


async def test_sync_due_resyncs_stale_servers(client, db_session, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers = await _tenant(client)
    stale_id = uuid.UUID((await _register(client, headers, name="stale")).json()["id"])
    fresh = (await _register(client, headers, name="fresh")).json()
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await db_session.execute(
        update(McpServer).where(McpServer.id == stale_id).values(synced_at=long_ago)
    )
    await db_session.commit()

    mcp_connect[SHOP_URL] = mcp_client.McpError("could not reach the server", sent=False)
    assert await sync_due(lambda: nullcontext(db_session)) >= 1

    stale = (await client.get(f"/mcp-servers/{stale_id}", headers=headers)).json()
    assert stale["status"] == "error" and len(stale["tools"]) == 2
    assert datetime.fromisoformat(stale["synced_at"]) > long_ago
    still = (await client.get(f"/mcp-servers/{fresh['id']}", headers=headers)).json()
    assert still["status"] == "ok" and still["synced_at"] == fresh["synced_at"]


async def test_call_tool_passes_meta_and_returns_text(mcp_connect):  # noqa: F811
    shop = fake_server()

    @shop.tool()
    async def sold_out() -> str:
        raise ToolError("sem estoque")

    mcp_connect[SHOP_URL] = shop
    server = McpServer(name="shop", url=SHOP_URL, timeout_seconds=5)
    meta = {"parley/user_id": "u-1", "parley/call_id": "c-1"}

    result = await mcp_client.call_tool(server, "search_product", {"query": "cartolina"}, meta)
    assert result.is_error is False and "cartolina" in result.text
    assert shop.calls[0]["meta"]["parley/user_id"] == "u-1"
    assert shop.calls[0]["meta"]["parley/call_id"] == "c-1"

    refused = await mcp_client.call_tool(server, "sold_out", {}, {})
    assert refused.is_error is True and "sem estoque" in refused.text

    mcp_connect[SHOP_URL] = mcp_client.McpError("could not reach the server", sent=False)
    try:
        await mcp_client.call_tool(server, "search_product", {"query": "x"}, {})
    except mcp_client.McpError as exc:
        assert exc.reason == "could not reach the server"
    else:
        raise AssertionError("expected McpError")


def test_may_have_sent_tells_never_sent_from_unknown_outcome():
    """The typed flag behind `McpError.sent` (no reason text is matched)."""
    import httpx2

    from app.tools.ssrf import SSRFError

    sent = mcp_client.may_have_sent
    assert sent(httpx2.ConnectError("refused")) is False
    assert sent(httpx2.ConnectTimeout("slow connect")) is False
    assert sent(SSRFError("private address")) is False
    assert sent(httpx2.ReadTimeout("no answer")) is True
    assert sent(httpx2.ReadError("connection broke")) is True
    assert sent(TimeoutError()) is True
    assert sent(ExceptionGroup("g", [httpx2.ConnectError("refused")])) is False
    refused = mcp_client._HttpState(statuses=[401])
    assert sent(httpx2.ReadError("closed"), refused) is False
    assert mcp_client.McpError("x").sent is True
    assert mcp_client.McpError("x", sent=False).sent is False
