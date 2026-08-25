"""Long-term memory: the `remember` built-in tool end-to-end, REST CRUD, tenant
isolation, and the `MemoryService` methods those two surfaces share.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.contact import Contact
from app.models.memory import Memory, MemoryType
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.base import ToolContext
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, **agent_overrides) -> tuple[dict, str, str]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant_response.json()["api_key"]}
    agent_payload = {
        "name": "Bot",
        "instructions": "Você é o atendente da Acme.",
        "model": "gpt-4o-mini",
        **agent_overrides,
    }
    agent = await client.post("/agents", json=agent_payload, headers=headers)
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"], agent.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _run_turn(fake_redis, db_session, runtime) -> None:
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _contact_id(client, headers) -> str:
    contacts = await client.get("/contacts", headers=headers)
    assert contacts.status_code == 200
    return contacts.json()[0]["id"]


async def test_remember_tool_stores_memory_and_shows_up_in_next_turn_context(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client)
    await _send(client, headers, channel_id, "meu email é ana@example.com")
    await _drain_jobs(fake_redis)
    contact_id = uuid.UUID(await _contact_id(client, headers))

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="remember",
                        arguments={
                            "type": "fact",
                            "content": "Email do contato: ana@example.com",
                            "importance": 4,
                        },
                    )
                ]
            ),
            LLMResponse(content="Anotado!"),
        ]
    )
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )

    result = await db_session.execute(select(Memory).where(Memory.contact_id == contact_id))
    memories = list(result.scalars().all())
    assert len(memories) == 1
    memory = memories[0]
    assert memory.type == MemoryType.FACT
    assert memory.content == "Email do contato: ana@example.com"
    assert memory.importance == 4

    # Next turn: the fact must be in the system prompt built for the model.
    await _send(client, headers, channel_id, "oi de novo")
    await _drain_jobs(fake_redis)
    provider2 = FakeLLMProvider([LLMResponse(content="Oi de novo!")])
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider2, registry=ToolRegistry([BuiltinExecutor()])),
    )

    system_prompt = provider2.calls[0]["messages"][0].content
    assert "## Known facts about this contact" in system_prompt
    assert "Email do contato: ana@example.com" in system_prompt


async def test_cross_tenant_cannot_read_or_delete_memory(client, fake_redis, db_session):
    headers_a, channel_a, _ = await _setup(client)
    await _send(client, headers_a, channel_a, "oi")
    await _drain_jobs(fake_redis)
    contact_id = await _contact_id(client, headers_a)

    created = await client.post(
        f"/contacts/{contact_id}/memories",
        json={"type": "fact", "content": "Gosta de pizza", "importance": 3},
        headers=headers_a,
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]

    headers_b, _, _ = await _setup(client)

    resp = await client.get(f"/contacts/{contact_id}/memories", headers=headers_b)
    assert resp.status_code == 404

    resp = await client.delete(f"/contacts/{contact_id}/memories/{memory_id}", headers=headers_b)
    assert resp.status_code == 404

    resp = await client.get(f"/contacts/{contact_id}/memories", headers=headers_a)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_rest_crud_create_list_search_delete(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)
    contact_id = await _contact_id(client, headers)

    low = await client.post(
        f"/contacts/{contact_id}/memories",
        json={"type": "preference", "content": "Prefere contato por email", "importance": 2},
        headers=headers,
    )
    high = await client.post(
        f"/contacts/{contact_id}/memories",
        json={"type": "fact", "content": "Cliente VIP desde 2020", "importance": 5},
        headers=headers,
    )
    assert low.status_code == 201, low.text
    assert high.status_code == 201, high.text

    listed = await client.get(f"/contacts/{contact_id}/memories", headers=headers)
    assert listed.status_code == 200
    assert [m["content"] for m in listed.json()] == [
        "Cliente VIP desde 2020",
        "Prefere contato por email",
    ]

    searched = await client.get(
        f"/contacts/{contact_id}/memories", params={"query": "VIP"}, headers=headers
    )
    assert [m["content"] for m in searched.json()] == ["Cliente VIP desde 2020"]

    delete_resp = await client.delete(
        f"/contacts/{contact_id}/memories/{high.json()['id']}", headers=headers
    )
    assert delete_resp.status_code == 204

    remaining = await client.get(f"/contacts/{contact_id}/memories", headers=headers)
    assert [m["content"] for m in remaining.json()] == ["Prefere contato por email"]

    missing_delete = await client.delete(
        f"/contacts/{contact_id}/memories/{high.json()['id']}", headers=headers
    )
    assert missing_delete.status_code == 404


async def test_expired_memories_are_excluded(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)
    contact_id = await _contact_id(client, headers)

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    expired = await client.post(
        f"/contacts/{contact_id}/memories",
        json={"type": "fact", "content": "Promoção expirada", "importance": 5, "expires_at": past},
        headers=headers,
    )
    assert expired.status_code == 201, expired.text

    listed = await client.get(f"/contacts/{contact_id}/memories", headers=headers)
    assert listed.json() == []


async def test_memory_disabled_agent_does_not_advertise_or_execute_remember(
    client, fake_redis, db_session
):
    # `handoff_enabled=False` too: ticket 08 adds a second built-in tool
    # (`handoff_to_human`), gated independently — without this the agent would
    # still advertise it and `tools == []` below would no longer hold.
    headers, channel_id, agent_id = await _setup(
        client, memory_enabled=False, handoff_enabled=False
    )
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="oi!")])
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )
    assert provider.calls[0]["tools"] == []

    contact_id = uuid.UUID(await _contact_id(client, headers))
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    ctx = ToolContext(
        session=db_session,
        tenant_id=agent.tenant_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        agent=agent,
        agent_run_id=uuid.uuid4(),
    )
    result = await BuiltinExecutor().execute(
        "remember", {"type": "fact", "content": "x"}, ctx
    )
    assert result.ok is False
    assert "disabled" in result.error


async def test_service_forget_is_tenant_scoped(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)
    contact_id = uuid.UUID(await _contact_id(client, headers))
    contact = await db_session.get(Contact, contact_id)
    tenant_id = contact.tenant_id

    service = MemoryService(db_session)
    memory = await service.store(tenant_id, contact_id, type="fact", content="x", importance=3)

    other_tenant_id = uuid.uuid4()
    assert await service.forget(other_tenant_id, memory.id) is False
    assert await service.forget(tenant_id, memory.id) is True
    assert await service.forget(tenant_id, memory.id) is False
