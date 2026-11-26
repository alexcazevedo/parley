"""PRODUCT.md's success demo for business tools over MCP: the example project's
MCP server wired in process, an agent attached to it, a customer on an external
channel asking for cartolina and picking the first, a second customer, and a
repeated call id. Only the model is scripted."""

import os
import uuid

import httpx
from mcp import Client

from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from examples.project_inbox import tools
from examples.project_inbox.app import create_app
from tests.mcp_fakes import SHOP_URL, mcp_connect  # noqa: F401 - fixture
from tests.test_mcp_tools import (
    ADD,
    SEARCH,
    _calls,
    _rows,
    _runtime,
    _tool_messages,
)

FIRST = "cartolina-branca-150g"
SECOND = "cartolina-branca-180g"
LISTING = "Temos Cartolina branca 150g e Cartolina branca 180g. Qual você prefere?"
CONFIRMATION = "Pronto, adicionei a Cartolina branca 150g ao seu orçamento."


async def _send(client, fake_redis, headers, channel_id, user_id, text) -> uuid.UUID:
    sent = await client.post(
        "/channels/external/messages",
        json={"channel_id": channel_id, "user_id": user_id, "message": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    return uuid.UUID(sent.json()["conversation_id"])


async def test_success_demo(client, fake_redis, db_session, test_engine, tmp_path, mcp_connect):  # noqa: F811
    inbox = create_app(
        tmp_path / "inbox.json",
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))),
        catch_up_seconds=3600,
    )
    mcp_connect[SHOP_URL] = tools.server

    # One call registers the example's server; the tools are listed.
    tenant = await client.post(
        "/tenants", json={"name": "Papelaria"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    registered = await client.post(
        "/mcp-servers", json={"name": "shop", "url": SHOP_URL}, headers=headers
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["status"] == "ok"
    assert {t["exposed_name"] for t in registered.json()["tools"]} == {SEARCH, ADD}

    agent = await client.post(
        "/agents",
        json={
            "name": "Vendedora",
            "instructions": "Você atende a papelaria.",
            "model": "m",
            "mcp_servers": ["shop"],
            "boundary_check_enabled": False,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={
            "type": "external",
            "name": "Inbox",
            "agent_id": agent.json()["id"],
            "config": {"events_url": "https://inbox.example.com/events"},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    channel_id = channel.json()["id"]

    # Customer one asks; the agent searches and names both products.
    conv_ana = await _send(
        client, fake_redis, headers, channel_id, "ana", "Vocês têm cartolina branca?"
    )
    provider = FakeLLMProvider(
        [_calls((SEARCH, {"query": "cartolina branca"})), LLMResponse(content=LISTING)]
    )
    conversation = await db_session.get(Conversation, conv_ana)
    assert conversation is not None
    reply = await _runtime(provider).run_turn(
        db_session, conversation, "Vocês têm cartolina branca?"
    )
    assert reply == LISTING
    result = _tool_messages(provider)[0].content
    assert FIRST in result and SECOND in result

    # "Sim, a primeira": the agent adds the first product.
    provider = FakeLLMProvider(
        [_calls((ADD, {"product_id": FIRST, "quantity": 1})), LLMResponse(content=CONFIRMATION)]
    )
    reply = await _runtime(provider).run_turn(db_session, conversation, "Sim, a primeira")
    assert reply == CONFIRMATION

    # The example shows the item in her quote and no other.
    async def quotes() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(inbox), base_url="http://t"
        ) as h:
            return (await h.get("/quotes")).json()

    assert await quotes() == {
        "ana": [{"product_id": FIRST, "name": "Cartolina branca 150g", "quantity": 1}]
    }

    # Both calls are in tool_executions with the customer context.
    rows = await _rows(test_engine, conv_ana)
    assert [r.tool_name for r in rows] == [SEARCH, ADD]
    for row in rows:
        assert row.context == {
            "conversation_id": str(conv_ana),
            "channel_id": channel_id,
            "user_id": "ana",
        }

    # The same add with the same call id adds nothing.
    async with Client(tools.server) as c:
        again = await c.call_tool(
            "add_to_quote",
            {"product_id": FIRST, "quantity": 1},
            meta={"parley/user_id": "ana", "parley/call_id": str(rows[1].call_id)},
        )
    assert not again.is_error
    assert (await quotes())["ana"][0]["quantity"] == 1

    # A second customer gets their own quote.
    conv_bia = await _send(client, fake_redis, headers, channel_id, "bia", "Sim, a primeira")
    other = await db_session.get(Conversation, conv_bia)
    assert other is not None and other.id != conv_ana
    provider = FakeLLMProvider(
        [_calls((ADD, {"product_id": SECOND, "quantity": 2})), LLMResponse(content="Pronto.")]
    )
    await _runtime(provider).run_turn(db_session, other, "Sim, a primeira")
    final = await quotes()
    assert set(final) == {"ana", "bia"}
    assert final["ana"] == [{"product_id": FIRST, "name": "Cartolina branca 150g", "quantity": 1}]
    assert final["bia"] == [{"product_id": SECOND, "name": "Cartolina branca 180g", "quantity": 2}]
