"""Manifest apply: one declarative file of agents, departments and tools,
validated like the API, applied all or nothing, idempotent."""

import copy
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import respx
from sqlalchemy import event, func, select

import app.manifest.apply as apply_module
from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.category import Category
from app.models.manifest_item import ManifestItem
from app.models.message import Message, MessageRole
from app.models.tool import Tool
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

TOKEN = "s3cr3t-manifest-token"
ORDERS_URL = "https://orders.example.com/orders"
DEBOUNCE_SECONDS = 0.2

MANIFEST = {
    "version": 1,
    "agents": [
        {
            "key": "main",
            "name": "Loja",
            "instructions": "Você é o atendente da Loja.",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "max_tokens": 512,
            "tool_names": ["get_order", "remember"],
        }
    ],
    "departments": [
        {"name": "billing", "description": "Pagamentos e boletos"},
        {"name": "shipping", "description": "Entregas"},
    ],
    "tools": [
        {
            "name": "get_order",
            "description": "Look up an order by id",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
            "endpoint": ORDERS_URL,
            "method": "GET",
            "auth_config": {"type": "bearer", "token": {"secret": "orders_token"}},
            "timeout_seconds": 5,
        }
    ],
}


def _manifest(**changes) -> dict:
    data = copy.deepcopy(MANIFEST)
    data.update(changes)
    return data


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    headers = {"X-API-Key": body["api_key"]}
    stored = await client.put("/secrets/orders_token", json={"value": TOKEN}, headers=headers)
    assert stored.status_code == 204
    return headers, uuid.UUID(body["id"])


async def _apply(client, headers, data, *, dry_run=False):
    response = await client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=data, headers=headers
    )
    assert TOKEN not in response.text
    return response


def _actions(body) -> list[tuple[str, str, str, list[str]]]:
    return [(c["kind"], c["key"], c["action"], c["fields"]) for c in body["changes"]]


async def _count(db_session, model, tenant_id) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )


async def _agent(db_session, tenant_id, key="main") -> Agent:
    item_id = await db_session.scalar(
        select(ManifestItem.item_id).where(
            ManifestItem.tenant_id == tenant_id,
            ManifestItem.kind == "agent",
            ManifestItem.key == key,
        )
    )
    agent = await db_session.get(Agent, item_id)
    await db_session.refresh(agent)
    return agent


@asynccontextmanager
async def _writes(test_engine):
    """Collects every INSERT/UPDATE/DELETE the engine runs inside the block."""
    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def test_first_apply_creates_everything_and_the_agent_answers(
    client, fake_redis, db_session
):
    headers, tenant_id = await _tenant(client)
    response = await _apply(client, headers, MANIFEST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False and body["generated"] == {}
    assert _actions(body) == [
        ("department", "billing", "create", []),
        ("department", "shipping", "create", []),
        ("tool", "get_order", "create", []),
        ("agent", "main", "create", []),
    ]
    assert await _count(db_session, ManifestItem, tenant_id) == 4
    stored = await db_session.scalar(select(Tool).where(Tool.tenant_id == tenant_id))
    assert stored.auth_config_encrypted and TOKEN not in stored.auth_config_encrypted

    agent = await _agent(db_session, tenant_id)
    assert agent.name == "Loja" and agent.tool_names == ["get_order", "remember"]
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": str(agent.id)},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u1", "message": "pedido 12345?"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation_id = uuid.UUID(sent.json()["conversation_id"])
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "12345"})]
            ),
            LLMResponse(content="Seu pedido 12345 já foi enviado."),
        ]
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ORDERS_URL).respond(json={"status": "shipped"})
        await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert [s.name for s in provider.calls[0]["tools"]][0] == "get_order"

    messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
    ).scalars().all()
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Seu pedido 12345 já foi enviado."
    assert any(m.role == MessageRole.TOOL and "shipped" in m.content for m in messages)


async def test_second_apply_is_unchanged_and_writes_nothing(client, db_session, test_engine):
    headers, tenant_id = await _tenant(client)
    assert (await _apply(client, headers, MANIFEST)).status_code == 200
    token_before = await db_session.scalar(
        select(Tool.auth_config_encrypted).where(Tool.tenant_id == tenant_id)
    )

    async with _writes(test_engine) as writes:
        response = await _apply(client, headers, MANIFEST)
    assert response.status_code == 200, response.text
    assert {c["action"] for c in response.json()["changes"]} == {"unchanged"}
    assert all(c["fields"] == [] for c in response.json()["changes"])
    assert writes == []
    db_session.expire_all()
    token_after = await db_session.scalar(
        select(Tool.auth_config_encrypted).where(Tool.tenant_id == tenant_id)
    )
    assert token_after == token_before
    assert await _count(db_session, ManifestItem, tenant_id) == 4


async def test_every_change_carries_the_id_of_its_item(client, db_session):
    headers, tenant_id = await _tenant(client)
    dry = await _apply(client, headers, MANIFEST, dry_run=True)
    assert [c["id"] for c in dry.json()["changes"]] == [None] * len(dry.json()["changes"])
    applied = await _apply(client, headers, MANIFEST)
    assert applied.status_code == 200, applied.text
    agent = await _agent(db_session, tenant_id)
    ids = {(c["kind"], c["key"]): c["id"] for c in applied.json()["changes"]}
    assert all(ids.values())
    assert ids[("agent", "main")] == str(agent.id)
    again = await _apply(client, headers, MANIFEST)
    assert {(c["kind"], c["key"]): c["id"] for c in again.json()["changes"]} == ids
    emptied = await _apply(client, headers, {"version": 1}, dry_run=True)
    assert {c["id"] for c in emptied.json()["changes"]} == set(ids.values())
    emptied = await _apply(client, headers, {"version": 1})
    assert emptied.status_code == 200, emptied.text
    assert {c["action"] for c in emptied.json()["changes"]} == {"delete"}
    assert {c["id"] for c in emptied.json()["changes"]} == {None}


async def test_changing_the_persona_is_an_update_of_instructions(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, MANIFEST)
    data = _manifest()
    data["agents"][0]["instructions"] = "Você é a atendente simpática da Loja."
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    assert ("agent", "main", "update", ["instructions"]) in _actions(response.json())
    agent = await _agent(db_session, tenant_id)
    assert [c for c in response.json()["changes"] if c["action"] != "unchanged"] == [
        {
            "kind": "agent",
            "key": "main",
            "action": "update",
            "fields": ["instructions"],
            "id": str(agent.id),
        }
    ]
    assert agent.instructions == "Você é a atendente simpática da Loja."
    assert await _count(db_session, Agent, tenant_id) == 1


async def test_a_field_left_out_resets_to_its_default(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, MANIFEST)
    data = _manifest()
    del data["agents"][0]["temperature"]
    del data["agents"][0]["max_tokens"]
    del data["tools"][0]["timeout_seconds"]
    del data["tools"][0]["auth_config"]
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    changed = {(c["kind"], c["key"]): c["fields"] for c in response.json()["changes"]}
    assert changed[("agent", "main")] == ["temperature", "max_tokens"]
    assert changed[("tool", "get_order")] == ["auth_config", "timeout_seconds"]
    agent = await _agent(db_session, tenant_id)
    assert agent.temperature == 0.7 and agent.max_tokens == 1024
    tool = await db_session.scalar(select(Tool).where(Tool.tenant_id == tenant_id))
    await db_session.refresh(tool)
    assert tool.timeout_seconds == 10 and tool.auth_config_encrypted is None


async def test_dry_run_reports_and_writes_nothing(client, db_session):
    headers, tenant_id = await _tenant(client)
    response = await _apply(client, headers, MANIFEST, dry_run=True)
    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True
    assert {c["action"] for c in response.json()["changes"]} == {"create"}
    for model in (Agent, Category, Tool, ManifestItem):
        assert await _count(db_session, model, tenant_id) == 0

    await _apply(client, headers, MANIFEST)
    data = _manifest()
    data["agents"][0]["name"] = "Outra"
    dry = await _apply(client, headers, data, dry_run=True)
    assert ("agent", "main", "update", ["name"]) in _actions(dry.json())
    assert (await _agent(db_session, tenant_id)).name == "Loja"


async def test_every_problem_is_returned_at_once_with_paths(client, db_session):
    headers, tenant_id = await _tenant(client)
    data = _manifest()
    data["agents"][0]["temperature"] = 5
    data["agents"][0]["colour"] = "blue"
    del data["tools"][0]["endpoint"]
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert sorted(d["field"] for d in error["details"]) == [
        "agents[0].colour",
        "agents[0].temperature",
        "tools[0].endpoint",
    ]
    assert all(set(d) == {"field", "message"} for d in error["details"])

    data = _manifest()
    data["agents"].append({**data["agents"][0]})
    data["agents"][0]["tool_names"] = ["no_such_tool"]
    data["tools"][0]["endpoint"] = "http://10.0.0.5/orders"
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    paths = sorted(d["field"] for d in response.json()["error"]["details"])
    assert paths == ["agents[0].tool_names[0]", "agents[1].key", "tools[0].endpoint"]
    for model in (Agent, Category, Tool, ManifestItem):
        assert await _count(db_session, model, tenant_id) == 0


async def test_a_failure_halfway_leaves_nothing_behind(client, db_session, monkeypatch):
    headers, tenant_id = await _tenant(client)

    async def explode(*args, **kwargs):
        raise RuntimeError("boom while creating the agent")

    monkeypatch.setitem(apply_module._EXECUTORS, "agent", explode)
    with pytest.raises(RuntimeError):
        await _apply(client, headers, MANIFEST)
    for model in (Agent, Category, Tool, ManifestItem):
        assert await _count(db_session, model, tenant_id) == 0


async def test_refused_exactly_as_the_api_refuses(client, db_session):
    headers, tenant_id = await _tenant(client)
    agent = {k: v for k, v in MANIFEST["agents"][0].items() if k not in ("key", "tool_names")}
    api = await client.post("/agents", json={**agent, "temperature": 3}, headers=headers)
    assert api.status_code == 422
    api_message = api.json()["error"]["details"][0]["message"]

    tool = {**MANIFEST["tools"][0], "endpoint": "http://127.0.0.1:8080/orders"}
    api_tool = await client.post("/tools", json=tool, headers=headers)
    assert api_tool.status_code == 422
    api_tool_message = api_tool.json()["error"]["message"]

    reserved = await client.post("/tools", json={**tool, "name": "remember"}, headers=headers)
    reserved_message = reserved.json()["error"]["details"][0]["message"]

    data = _manifest(tools=[tool])
    data["agents"][0]["temperature"] = 3
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "agents[0].temperature", "message": api_message}
    ]
    response = await _apply(client, headers, _manifest(tools=[tool]))
    assert response.json()["error"]["details"] == [
        {"field": "tools[0].endpoint", "message": api_tool_message}
    ]
    response = await _apply(
        client, headers, _manifest(tools=[{**MANIFEST["tools"][0], "name": "remember"}])
    )
    assert {"field": "tools[0].name", "message": reserved_message} in response.json()["error"][
        "details"
    ]
    for model in (Agent, Tool, ManifestItem):
        assert await _count(db_session, model, tenant_id) == 0


async def test_an_unmanaged_item_with_the_same_name_is_adopted(client, db_session):
    # Ticket 04 replaced the ticket 02 refusal with adoption.
    headers, tenant_id = await _tenant(client)
    made = await client.post("/categories", json={"name": "billing"}, headers=headers)
    response = await _apply(client, headers, MANIFEST)
    assert response.status_code == 200, response.text
    assert ("department", "billing", "adopt", ["description"]) in _actions(response.json())
    assert await _count(db_session, ManifestItem, tenant_id) == 4
    assert await _count(db_session, Category, tenant_id) == 2
    record = await db_session.scalar(
        select(ManifestItem).where(
            ManifestItem.tenant_id == tenant_id, ManifestItem.kind == "department"
        ).where(ManifestItem.key == "billing")
    )
    assert str(record.item_id) == made.json()["id"]


async def test_another_tenant_is_untouched(client, db_session):
    headers, tenant_id = await _tenant(client)
    other, other_id = await _tenant(client)
    hand_made = await client.post(
        "/tools", json={**MANIFEST["tools"][0], "description": "theirs"}, headers=other
    )
    assert hand_made.status_code == 201
    await client.post("/categories", json={"name": "billing"}, headers=other)

    assert (await _apply(client, headers, MANIFEST)).status_code == 200
    data = _manifest()
    data["tools"][0]["description"] = "changed"
    assert (await _apply(client, headers, data)).status_code == 200

    theirs = (await client.get("/tools", headers=other)).json()
    assert [(t["name"], t["description"]) for t in theirs] == [("get_order", "theirs")]
    assert await _count(db_session, ManifestItem, other_id) == 0
    assert await _count(db_session, Agent, other_id) == 0
    assert await _count(db_session, Category, other_id) == 1
    assert await _count(db_session, Tool, tenant_id) == 1


async def test_schema_and_auth(client):
    schema = (await client.get("/manifest/schema")).json()
    assert {"version", "agents", "departments", "tools"} <= set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ManifestAgent"]["additionalProperties"] is False
    assert "key" in schema["$defs"]["ManifestAgent"]["required"]
    assert (await client.post("/manifest/apply", json=MANIFEST)).status_code in (401, 403)


async def test_migration_0020_upgrades_and_downgrades():
    import asyncpg

    from tests.conftest import _ASYNCPG_ADMIN_DSN
    from tests.test_migration_open_conversation import _alembic

    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0020")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'manifest_items'"
            )
            assert {
                "ix_manifest_items_tenant_id",
                "uq_manifest_items_tenant_kind_key",
                "uq_manifest_items_tenant_kind_item",
            } <= {r["indexname"] for r in indexes}
        finally:
            await conn.close()
        _alembic(database_url, "downgrade", "0019")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval("SELECT to_regclass('manifest_items')") is None
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
