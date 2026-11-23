"""Memories: reading writes nothing, the same fact is stored once, and the
number given to the agent is a setting."""

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.llm.providers.fake import FakeLLMProvider
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.tools.registry import ToolRegistry


async def _setup(client, db_session):
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u1", "message": "oi"},
        headers=headers,
    )
    contact = (await client.get("/contacts", headers=headers)).json()[0]
    contact_row = await db_session.get(Contact, uuid.UUID(contact["id"]))
    return contact_row.tenant_id, contact_row.id, uuid.UUID(agent.json()["id"])


async def test_reading_memories_writes_nothing(client, db_session):
    tenant_id, contact_id, _ = await _setup(client, db_session)
    service = MemoryService(db_session)
    for i in range(3):
        await service.store(tenant_id, contact_id, "fact", f"fact {i}")
    await db_session.flush()

    memories = await service.get_contact_memories(tenant_id, contact_id)

    assert len(memories) == 3
    assert all(m.last_accessed_at is None for m in memories)
    assert not db_session.dirty
    assert not db_session.new


async def test_same_importance_newer_comes_first(client, db_session):
    tenant_id, contact_id, _ = await _setup(client, db_session)
    service = MemoryService(db_session)
    old = await service.store(tenant_id, contact_id, "fact", "old", importance=3)
    new = await service.store(tenant_id, contact_id, "fact", "new", importance=3)
    now = datetime.now(UTC)
    await db_session.execute(
        update(Memory).where(Memory.id == old.id).values(created_at=now - timedelta(hours=1))
    )
    await db_session.execute(update(Memory).where(Memory.id == new.id).values(created_at=now))
    db_session.expire_all()

    memories = await service.get_contact_memories(tenant_id, contact_id)

    assert [m.content for m in memories] == ["new", "old"]


async def test_store_dedupes_normalized_content_and_keeps_higher_importance(client, db_session):
    tenant_id, contact_id, _ = await _setup(client, db_session)
    service = MemoryService(db_session)

    first = await service.store(tenant_id, contact_id, "preference", "Prefers  e-mail", 3)
    second = await service.store(tenant_id, contact_id, "preference", " prefers e-mail ", 5)
    assert second.id == first.id
    assert first.importance == 5

    third = await service.store(tenant_id, contact_id, "preference", "PREFERS\te-mail", 1)
    assert third.id == first.id
    assert first.importance == 5

    rows = await service.get_contact_memories(tenant_id, contact_id)
    assert len(rows) == 1
    assert rows[0].content == "Prefers  e-mail"


async def test_store_same_content_for_other_contact_or_after_expiry_is_new(client, db_session):
    tenant_id, contact_id, _ = await _setup(client, db_session)
    service = MemoryService(db_session)

    mine = await db_session.get(Contact, contact_id)
    other = Contact(
        tenant_id=tenant_id, channel_id=mine.channel_id, external_user_id="other"
    )
    db_session.add(other)
    await db_session.flush()

    first = await service.store(tenant_id, contact_id, "fact", "likes tea")
    for_other = await service.store(tenant_id, other.id, "fact", "likes tea")
    assert for_other.id != first.id

    first.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()
    again = await service.store(tenant_id, contact_id, "fact", "likes tea")
    assert again.id != first.id


async def test_agent_context_holds_memory_context_limit_memories(
    client, db_session, monkeypatch
):
    tenant_id, contact_id, agent_id = await _setup(client, db_session)
    service = MemoryService(db_session)
    for i in range(4):
        await service.store(tenant_id, contact_id, "fact", f"fact {i}")
    await db_session.flush()

    patched = get_settings().model_copy(update={"memory_context_limit": 2})
    monkeypatch.setattr("app.agents.runtime.get_settings", lambda: patched)

    agent = await db_session.get(Agent, agent_id)
    conversation = (
        await db_session.execute(
            select(Conversation).where(Conversation.contact_id == contact_id)
        )
    ).scalar_one()
    runtime = AgentRuntime(
        provider=FakeLLMProvider([]), registry=ToolRegistry([])
    )
    memories = await runtime._load_memories(db_session, agent, conversation)

    assert len(memories) == 2
