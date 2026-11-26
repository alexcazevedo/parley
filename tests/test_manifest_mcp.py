"""Manifest MCP servers: set up from the file, attached to agents, synced after
the commit, adopted, updated, removed, exported; the token only by secret name."""

import copy
import json
import os
import uuid

import structlog
from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.manifest.schema import MAX_MCP_SERVERS
from app.models.conversation import Conversation
from app.models.manifest_item import ManifestItem
from app.models.mcp_server import McpServer, McpServerStatus
from app.models.mcp_tool import McpTool
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.security import secrets as secrets_crypto
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from tests.mcp_fakes import SHOP_URL, fake_server, mcp_connect  # noqa: F401

TOKEN = "tok-for-the-shop-server"
TOKEN_2 = "tok-replaced-for-the-shop"
SECRETS = (TOKEN, TOKEN_2)
CREDENTIALS_MESSAGE = 'credentials must be secret references; use {"secret": "<name>"}'

MANIFEST = {
    "version": 1,
    "agents": [
        {
            "key": "main",
            "name": "Loja",
            "instructions": "Você atende a loja.",
            "model": "gpt-4o-mini",
            "mcp_servers": ["shop"],
            "boundary_check_enabled": False,
        }
    ],
    "mcp_servers": [
        {
            "name": "shop",
            "url": SHOP_URL,
            "auth_config": {"type": "bearer", "token": {"secret": "shop_token"}},
            "allowed_tools": ["search_product", "add_to_quote"],
            "timeout_seconds": 15,
        }
    ],
    "channels": [{"key": "site", "type": "webchat", "name": "Site", "agent": "main"}],
}


def _manifest() -> dict:
    return copy.deepcopy(MANIFEST)


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    return {"X-API-Key": body["api_key"]}, uuid.UUID(body["id"])


async def _put(client, headers, name, value):
    response = await client.put(f"/secrets/{name}", json={"value": value}, headers=headers)
    assert response.status_code == 204, response.text


async def _apply(client, headers, data, *, dry_run=False):
    response = await client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=data, headers=headers
    )
    for value in SECRETS:
        assert value not in response.text
    return response


def _actions(body) -> dict[tuple[str, str], tuple[str, list[str]]]:
    return {(c["kind"], c["key"]): (c["action"], c["fields"]) for c in body["changes"]}


async def _count(db_session, model, tenant_id) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )


async def _server(db_session, tenant_id, name="shop") -> McpServer | None:
    server = await db_session.scalar(
        select(McpServer).where(McpServer.tenant_id == tenant_id, McpServer.name == name)
    )
    if server is not None:
        await db_session.refresh(server)
    return server


async def test_apply_imports_the_tools_and_a_turn_calls_one(
    client, fake_redis, db_session, mcp_connect  # noqa: F811
):
    shop = fake_server()
    mcp_connect[SHOP_URL] = shop
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)

    dry = await _apply(client, headers, _manifest(), dry_run=True)
    assert dry.status_code == 200, dry.text
    assert _actions(dry.json())[("mcp_server", "shop")] == ("create", [])
    assert await _count(db_session, McpServer, tenant_id) == 0

    with structlog.testing.capture_logs() as logs:
        response = await _apply(client, headers, _manifest())
    assert response.status_code == 200, response.text
    assert _actions(response.json())[("mcp_server", "shop")] == ("create", [])
    assert TOKEN not in json.dumps(logs, default=str)

    server = await _server(db_session, tenant_id)
    assert server.status == McpServerStatus.OK
    assert server.timeout_seconds == 15
    assert TOKEN not in server.auth_config_encrypted
    assert json.loads(secrets_crypto.decrypt(server.auth_config_encrypted)) == {
        "type": "bearer",
        "token": TOKEN,
    }
    tools = (await db_session.scalars(select(McpTool).where(McpTool.server_id == server.id))).all()
    assert {t.name for t in tools} == {"search_product", "add_to_quote"}

    again = await _apply(client, headers, _manifest())
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}

    channel = await client.get("/channels", headers=headers)
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()[0]["id"], "user_id": "u1", "message": "cartolina?"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    conversation = await db_session.get(Conversation, uuid.UUID(sent.json()["conversation_id"]))
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="shop__search_product", arguments={"query": "azul"})
                ]
            ),
            LLMResponse(content="Achei."),
        ]
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry())
    assert await runtime.run_turn(db_session, conversation, "cartolina?") == "Achei."
    assert [c["name"] for c in shop.calls] == ["search_product"]


async def test_a_plain_token_is_refused_and_a_missing_secret_is_named(client, mcp_connect):  # noqa: F811
    mcp_connect[SHOP_URL] = fake_server()
    headers, _ = await _tenant(client)

    data = _manifest()
    data["mcp_servers"][0]["auth_config"] = {"type": "bearer", "token": TOKEN}
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert {"field": "mcp_servers[0].auth_config", "message": CREDENTIALS_MESSAGE} in details

    response = await _apply(client, headers, _manifest())
    assert response.status_code == 422
    assert {
        "field": "mcp_servers[0].auth_config.token",
        "message": "secret 'shop_token' is not set",
    } in response.json()["error"]["details"]

    data = _manifest()
    data["mcp_servers"][0]["extra"] = 1
    assert (await _apply(client, headers, data)).status_code == 422


async def test_an_unknown_server_is_a_problem_and_nothing_is_written(
    client, db_session, mcp_connect  # noqa: F811
):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)
    data = _manifest()
    data["agents"][0]["mcp_servers"] = ["shop", "ghost"]
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert {
        "field": "agents[0].mcp_servers[1]",
        "message": "unknown MCP server: not in the file and not a server of the tenant",
    } in response.json()["error"]["details"]
    assert await _count(db_session, McpServer, tenant_id) == 0
    assert await _count(db_session, ManifestItem, tenant_id) == 0

    # A server of the tenant, made by hand, is known.
    created = await client.post(
        "/mcp-servers", json={"name": "ghost", "url": "https://ghost.example.com/mcp"},
        headers=headers,
    )
    assert created.status_code == 201
    assert (await _apply(client, headers, data)).status_code == 200


async def test_removing_the_server_deletes_it_and_its_tools(
    client, db_session, mcp_connect  # noqa: F811
):
    mcp_connect[SHOP_URL] = fake_server()
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)
    assert (await _apply(client, headers, _manifest())).status_code == 200
    assert await _count(db_session, McpTool, tenant_id) == 2

    # An agent still listing the removed server is a problem.
    data = _manifest()
    del data["mcp_servers"][0]
    assert (await _apply(client, headers, data)).status_code == 422
    assert await _count(db_session, McpServer, tenant_id) == 1

    data["agents"][0]["mcp_servers"] = []
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    assert _actions(response.json())[("mcp_server", "shop")][0] == "delete"
    assert await _count(db_session, McpServer, tenant_id) == 0
    assert await _count(db_session, McpTool, tenant_id) == 0
    assert await _count(db_session, ManifestItem, tenant_id) == 2  # agent, channel


async def test_a_hand_made_server_is_adopted_and_a_file_without_the_section_leaves_it(
    client, db_session, mcp_connect  # noqa: F811
):
    mcp_connect[SHOP_URL] = fake_server()
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)
    made = await client.post(
        "/mcp-servers", json={"name": "shop", "url": SHOP_URL}, headers=headers
    )
    assert made.status_code == 201
    old = _manifest()
    del old["mcp_servers"]
    old["agents"][0]["mcp_servers"] = ["shop"]
    assert (await _apply(client, headers, old)).status_code == 200  # an older file
    assert await _count(db_session, McpServer, tenant_id) == 1

    response = await _apply(client, headers, _manifest())
    assert response.status_code == 200, response.text
    action, fields = _actions(response.json())[("mcp_server", "shop")]
    assert action == "adopt"
    assert set(fields) == {"auth_config", "allowed_tools", "timeout_seconds"}
    server = await _server(db_session, tenant_id)
    assert str(server.id) == made.json()["id"]
    assert server.timeout_seconds == 15

    assert (await _apply(client, headers, old)).status_code == 200
    assert await _server(db_session, tenant_id) is not None  # not named: left alone


async def test_a_replaced_secret_is_an_update_and_a_dry_run_syncs_nothing(
    client, db_session, mcp_connect  # noqa: F811
):
    shop = fake_server()
    mcp_connect[SHOP_URL] = shop
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)
    assert (await _apply(client, headers, _manifest())).status_code == 200
    server = await _server(db_session, tenant_id)
    first_sync = server.synced_at

    await _put(client, headers, "shop_token", TOKEN_2)
    dry = await _apply(client, headers, _manifest(), dry_run=True)
    assert _actions(dry.json())[("mcp_server", "shop")] == ("update", ["auth_config"])
    assert (await _server(db_session, tenant_id)).synced_at == first_sync

    response = await _apply(client, headers, _manifest())
    assert _actions(response.json())[("mcp_server", "shop")] == ("update", ["auth_config"])
    server = await _server(db_session, tenant_id)
    assert server.synced_at > first_sync
    assert json.loads(secrets_crypto.decrypt(server.auth_config_encrypted))["token"] == TOKEN_2


async def test_a_failed_sync_is_the_servers_error_not_an_apply_failure(
    client, db_session, mcp_connect  # noqa: F811
):
    headers, tenant_id = await _tenant(client)  # no route for the URL: unreachable
    await _put(client, headers, "shop_token", TOKEN)
    response = await _apply(client, headers, _manifest())
    assert response.status_code == 200, response.text
    server = await _server(db_session, tenant_id)
    assert server.status == McpServerStatus.ERROR
    assert server.error == "could not reach the server"


async def test_export_then_apply_on_an_empty_tenant_reproduces_the_server(
    client, db_session, mcp_connect  # noqa: F811
):
    mcp_connect[SHOP_URL] = fake_server()
    headers, _ = await _tenant(client)
    await _put(client, headers, "shop_token", TOKEN)
    assert (await _apply(client, headers, _manifest())).status_code == 200

    exported = await client.get("/manifest", headers=headers)
    assert exported.status_code == 200
    for value in SECRETS:
        assert value not in exported.text
    body = exported.json()
    assert body["manifest"]["mcp_servers"] == _manifest()["mcp_servers"]
    assert body["secrets_needed"] == []

    other, other_id = await _tenant(client)
    await _put(client, other, "shop_token", TOKEN)
    response = await _apply(client, other, body["manifest"])
    assert response.status_code == 200, response.text
    server = await _server(db_session, other_id)
    assert server.status == McpServerStatus.OK and server.timeout_seconds == 15
    assert await _count(db_session, McpTool, other_id) == 2

    # An unmanaged server is exported too, its token as a name to set.
    made = await client.post(
        "/mcp-servers",
        json={
            "name": "zeta",
            "url": SHOP_URL,
            "auth_config": {"type": "header", "header_name": "X-Key", "token": TOKEN_2},
        },
        headers=other,
    )
    assert made.status_code == 201
    body = (await client.get("/manifest", headers=other)).json()
    assert TOKEN_2 not in json.dumps(body)
    assert [s["name"] for s in body["manifest"]["mcp_servers"]] == ["shop", "zeta"]
    assert body["manifest"]["mcp_servers"][1]["auth_config"] == {
        "type": "header",
        "header_name": "X-Key",
        "token": {"secret": "mcp_zeta_token"},
    }
    assert body["secrets_needed"] == ["mcp_zeta_token"]


async def test_the_cap_and_the_url_check(client, mcp_connect):  # noqa: F811
    headers, _ = await _tenant(client)
    data = {"version": 1, "mcp_servers": [{"name": f"s{i}", "url": SHOP_URL} for i in range(21)]}
    assert MAX_MCP_SERVERS == 20
    assert (await _apply(client, headers, data)).status_code == 422
    data = {"version": 1, "mcp_servers": [{"name": "bad", "url": "http://127.0.0.1/mcp"}]}
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "mcp_servers[0].url"
