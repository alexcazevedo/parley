"""Sessions: a customer message after a long silence starts a new session, and
the agent no longer reads the messages or the running summary of the earlier
one (app/conversations/sessions.py). Message times are set in the database.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import select, update

from app.agents.runtime import AgentRuntime
from app.conversations.sessions import begin_turn, current_session, session_first_seq
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.conversation_session import ConversationSession, SessionStatus
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
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
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            "boundary_check_enabled": False,
            "summary_threshold": 0,
            **agent_overrides,
        },
        headers=headers,
    )
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


async def _turn(fake_redis, db_session, provider) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([]))
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _age(db_session, conversation_id, hours: float, *, role: MessageRole | None = None):
    """Move the conversation's messages (of `role`, or all) `hours` into the past."""
    query = update(Message).where(Message.conversation_id == conversation_id)
    if role is not None:
        query = query.where(Message.role == role)
    await db_session.execute(
        query.values(created_at=Message.created_at - timedelta(hours=hours))
    )
    await db_session.commit()


async def _rows(db_session, conversation_id) -> list[ConversationSession]:
    result = await db_session.execute(
        select(ConversationSession)
        .where(ConversationSession.conversation_id == conversation_id)
        .order_by(ConversationSession.first_seq)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _messages(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(result.scalars().all())


def _prompt(provider, index: int = -1) -> str:
    return "\n".join(str(message.content) for message in provider.calls[index]["messages"])


async def test_agent_fields_defaults_and_bounds(client):
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    base = {"name": "Bot", "instructions": "x", "model": "m"}
    created = await client.post("/agents", json=base, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["session_memory_enabled"] is True
    assert body["session_gap_hours"] == 48
    assert body["session_recap_limit"] == 3
    assert body["session_recap_max_age_days"] == 90
    for field, bad in (
        ("session_gap_hours", 0),
        ("session_gap_hours", 721),
        ("session_recap_limit", 11),
        ("session_recap_limit", -1),
        ("session_recap_max_age_days", 0),
        ("session_recap_max_age_days", 3651),
    ):
        response = await client.post("/agents", json={**base, field: bad}, headers=headers)
        assert response.status_code == 422, (field, bad)
    patched = await client.patch(
        f"/agents/{body['id']}",
        json={"session_gap_hours": 2, "session_memory_enabled": None},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["session_gap_hours"] == 2
    assert patched.json()["session_memory_enabled"] is True


async def test_two_turns_a_day_apart_share_one_session(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    await _age(db_session, conversation_id, 24)

    await _send(client, headers, channel_id, "calabresa")
    await _turn(fake_redis, db_session, provider)

    prompt = _prompt(provider)
    assert "quero uma pizza grande" in prompt
    assert "Claro, qual sabor?" in prompt
    rows = await _rows(db_session, conversation_id)
    messages = await _messages(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.OPEN.value]
    assert rows[0].first_seq == messages[0].seq


async def test_turn_after_the_gap_opens_a_new_session(client, fake_redis, db_session):
    # Recaps off: the pending snapshot would stand in as a recap (ticket 03).
    headers, channel_id, _ = await _setup(client, session_recap_limit=0)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    conversation = await db_session.get(Conversation, conversation_id)
    conversation.summary = "RESUMO ANTIGO: cliente quer pizza"
    conversation.summary_last_seq = (await _messages(db_session, conversation_id))[-1].seq
    await db_session.commit()
    await _age(db_session, conversation_id, 49)
    old_last_seq = (await _messages(db_session, conversation_id))[-1].seq

    await _send(client, headers, channel_id, "oi de novo")
    await _turn(fake_redis, db_session, provider)

    prompt = _prompt(provider)
    assert "oi de novo" in prompt
    assert "quero uma pizza grande" not in prompt
    assert "Claro, qual sabor?" not in prompt
    assert "RESUMO ANTIGO" not in prompt
    rows = await _rows(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.PENDING.value, SessionStatus.OPEN.value]
    old, new = rows
    assert old.last_seq == old_last_seq
    assert old.ended_by == "silence"
    assert old.ended_at is not None
    assert old.summary_snapshot == "RESUMO ANTIGO: cliente quer pizza"
    new_message = next(
        m for m in await _messages(db_session, conversation_id) if m.content == "oi de novo"
    )
    assert new.first_seq == new_message.seq
    assert new.last_seq is None and new.ended_at is None
    await db_session.refresh(conversation)
    assert conversation.summary is None and conversation.summary_last_seq is None
    # No stored message was deleted.
    assert len(await _messages(db_session, conversation_id)) == 4


async def test_gap_counts_from_the_agent_reply(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    # The customer wrote 60 hours ago, but the agent answered 47 hours ago.
    await _age(db_session, conversation_id, 13, role=MessageRole.USER)
    await _age(db_session, conversation_id, 47)

    await _send(client, headers, channel_id, "calabresa")
    await _turn(fake_redis, db_session, provider)

    assert "quero uma pizza grande" in _prompt(provider)
    rows = await _rows(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.OPEN.value]


async def test_custom_gap_is_honoured(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, session_gap_hours=2)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    await _age(db_session, conversation_id, 3)

    await _send(client, headers, channel_id, "oi de novo")
    await _turn(fake_redis, db_session, provider)

    assert "quero uma pizza grande" not in _prompt(provider)
    rows = await _rows(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.PENDING.value, SessionStatus.OPEN.value]


async def test_running_summary_covers_only_the_new_session(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, summary_threshold=2)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.refresh(conversation)
    assert conversation.summary is not None  # the first session was summarized
    first_summary = conversation.summary
    await _age(db_session, conversation_id, 49)

    await _send(client, headers, channel_id, "oi de novo")
    await _turn(fake_redis, db_session, provider)

    # Reply, then the summarizer: it reads only the new session, and no prior summary.
    summarizer_prompt = _prompt(provider)
    assert "New messages:" in summarizer_prompt
    assert "oi de novo" in summarizer_prompt
    assert "quero uma pizza grande" not in summarizer_prompt
    assert "Prior summary" not in summarizer_prompt
    rows = await _rows(db_session, conversation_id)
    assert rows[0].summary_snapshot == first_summary
    await db_session.refresh(conversation)
    messages = await _messages(db_session, conversation_id)
    assert conversation.summary_last_seq == messages[-1].seq


async def test_switched_off_nothing_changes(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, session_memory_enabled=False)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    conversation = await db_session.get(Conversation, conversation_id)
    conversation.summary = "RESUMO ANTIGO"
    await db_session.commit()
    await _age(db_session, conversation_id, 24 * 30)

    await _send(client, headers, channel_id, "oi de novo")
    await _turn(fake_redis, db_session, provider)

    prompt = _prompt(provider)
    assert "quero uma pizza grande" in prompt
    assert "RESUMO ANTIGO" in prompt
    assert await _rows(db_session, conversation_id) == []
    await db_session.refresh(conversation)
    assert conversation.summary == "RESUMO ANTIGO"


async def test_existing_conversation_gets_its_first_row_lazily(
    client, fake_redis, db_session
):
    headers, channel_id, agent_id = await _setup(client, session_memory_enabled=False)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    assert await _rows(db_session, conversation_id) == []

    patched = await client.patch(
        f"/agents/{agent_id}", json={"session_memory_enabled": True}, headers=headers
    )
    assert patched.status_code == 200
    await _age(db_session, conversation_id, 1)
    await _send(client, headers, channel_id, "calabresa")
    await _turn(fake_redis, db_session, provider)

    assert "quero uma pizza grande" in _prompt(provider)
    rows = await _rows(db_session, conversation_id)
    messages = await _messages(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.OPEN.value]
    assert rows[0].first_seq == messages[0].seq
    conversation = await db_session.get(Conversation, conversation_id)
    assert rows[0].contact_id == conversation.contact_id
    assert rows[0].tenant_id == conversation.tenant_id


async def test_begin_turn_splits_once_for_the_same_batch(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    await _age(db_session, conversation_id, 49)
    await _send(client, headers, channel_id, "oi de novo")

    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, conversation.agent_id)
    batch = [m for m in await _messages(db_session, conversation_id) if m.content == "oi de novo"]
    first = await begin_turn(db_session, agent, conversation, batch)
    again = await begin_turn(db_session, agent, conversation, batch)

    assert first is not None and again is not None and first.id == again.id
    assert session_first_seq(first) == batch[0].seq
    rows = await _rows(db_session, conversation_id)
    assert [row.status for row in rows] == [SessionStatus.PENDING.value, SessionStatus.OPEN.value]


async def test_sessions_are_tenant_scoped(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero uma pizza grande")
    provider = FakeLLMProvider([LLMResponse(content="Claro, qual sabor?")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    conversation = await db_session.get(Conversation, conversation_id)
    assert (await current_session(db_session, conversation)) is not None

    other_headers, other_channel, _ = await _setup(client)
    other_id = await _send(client, other_headers, other_channel, "olá")
    other = await db_session.get(Conversation, other_id)
    assert other.tenant_id != conversation.tenant_id
    # The same conversation id seen from another tenant has no session.
    foreign = Conversation(
        id=conversation.id,
        tenant_id=other.tenant_id,
        contact_id=other.contact_id,
        channel_id=other.channel_id,
        agent_id=other.agent_id,
    )
    assert await current_session(db_session, foreign) is None
    assert session_first_seq(None) is None
