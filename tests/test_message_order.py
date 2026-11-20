"""Messages have one order (PRODUCT.md E4.8, ticket 01).

`messages.seq` (sequence `messages_seq`, taken at insert) is the order of a
conversation; `created_at` is only for display, because it comes from the
database for inbound rows and from the app clock for the others. The rolling
summary continues from `conversations.summary_last_seq`, and every insert of
a message (and the summarizer) locks the conversation row first so a row
with a lower `seq` cannot become visible after the summary moved past it.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import select

import app.api.conversations as conversations_api
from app.agents import sentiment
from app.agents.runtime import AgentRuntime
from app.channels.base import IncomingMessage
from app.conversations.summary import SUMMARIZER_SYSTEM_PROMPT, maybe_summarize
from app.handoff import summarizer as handoff_summarizer
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.messages.ingest import ingest
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import RedisQueue
from app.repositories.messages import MessagesRepository
from app.tools.registry import ToolRegistry
from app.workers.scheduler import _load_messages
from tests.conftest import _ASYNCPG_ADMIN_DSN, _TEST_DB_NAME

HOUR = timedelta(hours=1)


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
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"], agent.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


def _add(db_session, conversation: Conversation, role: MessageRole, content: str, created_at):
    message = Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.IN if role == MessageRole.USER else MessageDirection.OUT,
        role=role,
        type="text",
        content=content,
        created_at=created_at,
    )
    db_session.add(message)
    return message


async def _other_connection() -> asyncpg.Connection:
    return await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{_TEST_DB_NAME}")


async def test_reads_follow_insertion_order_whatever_the_clock_says(client, db_session):
    headers, channel_id, agent_id = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "u0")
    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, uuid.UUID(agent_id))

    now = datetime.now(UTC)
    # Assistant rows stamped by a clock one hour behind, then one hour ahead.
    for role, content, stamp in [
        (MessageRole.ASSISTANT, "a1", now - HOUR),
        (MessageRole.USER, "u2", now + timedelta(seconds=1)),
        (MessageRole.ASSISTANT, "a3", now + HOUR),
        (MessageRole.USER, "u4", now + timedelta(seconds=2)),
    ]:
        _add(db_session, conversation, role, content, stamp)
        await db_session.flush()
    expected = ["u0", "a1", "u2", "a3", "u4"]

    rows = await MessagesRepository(db_session).list_by_conversation(
        conversation.tenant_id, conversation.id
    )
    assert [m.content for m in rows] == expected
    seqs = [m.seq for m in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    runtime = AgentRuntime(provider=FakeLLMProvider([]), registry=ToolRegistry([]))
    history = await runtime._recent_messages(db_session, agent, conversation)
    assert [m.content for m in history] == expected

    sentiment_history = await sentiment._recent_messages(db_session, conversation)
    assert [m.content for m in sentiment_history] == expected

    prompt = await handoff_summarizer._build_prompt(db_session, agent, conversation, "reason")
    brief_input = prompt[-1].content
    positions = [brief_input.index(f": {content}") for content in expected]
    assert positions == sorted(positions)

    # The scheduler's batch: arrival order given as the reverse, seq wins.
    loaded = await _load_messages(db_session, [m.id for m in reversed(rows)])
    assert [m.content for m in loaded] == expected

    listed = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [m["content"] for m in listed.json()] == expected


async def test_summary_rounds_cover_every_message_exactly_once(client, db_session):
    headers, channel_id, agent_id = await _setup(client, summary_threshold=2)
    conversation_id = await _send(client, headers, channel_id, "user-0")
    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, uuid.UUID(agent_id))

    provider = FakeLLMProvider([LLMResponse(content="the summary")], repeat_last=True)
    contents = ["user-0"]
    skews = [-HOUR, HOUR, -HOUR]
    for round_number, skew in enumerate(skews, start=1):
        if round_number > 1:
            user = f"user-{round_number}"
            _add(db_session, conversation, MessageRole.USER, user, datetime.now(UTC))
            contents.append(user)
            await db_session.flush()
        assistant = f"assistant-{round_number}"
        _add(db_session, conversation, MessageRole.ASSISTANT, assistant, datetime.now(UTC) + skew)
        contents.append(assistant)
        await db_session.flush()
        await maybe_summarize(db_session, agent, conversation, provider)

    prompts = [
        call["messages"][-1].content
        for call in provider.calls
        if call["messages"][0].content == SUMMARIZER_SYSTEM_PROMPT
    ]
    assert len(prompts) == 3
    for content in contents:
        lines = [line for prompt in prompts for line in prompt.splitlines()]
        assert sum(line.endswith(f": {content}") for line in lines) == 1, (content, prompts)

    last_seq = await db_session.scalar(
        select(Message.seq)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.seq.desc())
        .limit(1)
    )
    assert conversation.summary_last_seq == last_seq


async def test_ingest_locks_the_conversation_row_before_inserting(
    client, db_session, fake_redis
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "first")
    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.commit()

    incoming = IncomingMessage(
        tenant_id=conversation.tenant_id,
        channel_id=conversation.channel_id,
        external_user_id="user-1",
        external_message_id=f"ext-{uuid.uuid4()}",
        type="text",
        content="second",
        timestamp=datetime.now(UTC),
    )
    result = await ingest(db_session, RedisQueue(fake_redis), incoming)
    assert result.conversation_id == conversation_id

    other = await _other_connection()
    try:
        # FOR NO KEY UPDATE does not conflict with the foreign-key check's own
        # FOR KEY SHARE, only with an explicit FOR UPDATE.
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await other.execute(
                "SELECT id FROM conversations WHERE id = $1 FOR NO KEY UPDATE NOWAIT",
                conversation_id,
            )
    finally:
        await other.close()
        await db_session.rollback()


async def test_operator_message_locks_the_conversation_row_before_inserting(
    client, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "hello")
    await db_session.commit()

    sent: list[str] = []

    class RecordingAdapter:
        async def send_message(self, message, contact, channel):
            sent.append(message.content)

    monkeypatch.setattr(conversations_api, "get_adapter", lambda _type: RecordingAdapter())

    other = await _other_connection()
    transaction = other.transaction()
    await transaction.start()
    released = False
    try:
        await other.execute(
            "SELECT id FROM conversations WHERE id = $1 FOR NO KEY UPDATE", conversation_id
        )
        request = asyncio.create_task(
            client.post(
                f"/conversations/{conversation_id}/messages",
                json={"content": "operator here"},
                headers=headers,
            )
        )
        await asyncio.sleep(0.5)
        # Still waiting for the row lock: nothing inserted. The send comes
        # first (the lock is never held across it), so it has already gone out.
        assert not request.done()
        assert sent == ["operator here"]
        stored = await other.fetchval(
            "SELECT count(*) FROM messages WHERE conversation_id = $1"
            " AND role::text ILIKE 'human_operator'",
            conversation_id,
        )
        assert stored == 0
        await transaction.rollback()
        released = True
        response = await asyncio.wait_for(request, timeout=10)
        assert response.status_code == 201, response.text
        assert sent == ["operator here"]
    finally:
        if not released:
            await transaction.rollback()
            await asyncio.wait_for(request, timeout=10)
        await other.close()


async def test_summarizer_locks_the_conversation_row_before_reading(client, db_session):
    headers, channel_id, agent_id = await _setup(client, summary_threshold=1)
    conversation_id = await _send(client, headers, channel_id, "hello")
    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await db_session.commit()

    provider = FakeLLMProvider([LLMResponse(content="the summary")], repeat_last=True)
    other = await _other_connection()
    transaction = other.transaction()
    await transaction.start()
    released = False
    try:
        await other.execute(
            "SELECT id FROM conversations WHERE id = $1 FOR NO KEY UPDATE", conversation_id
        )
        task = asyncio.create_task(maybe_summarize(db_session, agent, conversation, provider))
        await asyncio.sleep(0.5)
        # Waiting for the lock, before reading the tail: no model call yet.
        assert not task.done()
        assert provider.call_count == 0
        await transaction.rollback()
        released = True
        await asyncio.wait_for(task, timeout=10)
        assert provider.call_count == 1
    finally:
        if not released:
            await transaction.rollback()
            await asyncio.wait_for(task, timeout=10)
        await other.close()
    await db_session.commit()
