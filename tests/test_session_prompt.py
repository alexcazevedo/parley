"""What a new session reads: the facts, and the most recent recaps of earlier
sessions (app/agents/context.py, app/memory/service.py), never their messages.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents.context import RECAPS_HEADER
from app.agents.runtime import AgentRuntime
from app.handoff import summarizer as handoff_summarizer
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.conversation_session import ConversationSession, SessionStatus
from app.models.memory import Memory, MemoryType
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2


def _factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, **agent_overrides):
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
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
    return headers, channel.json()["id"]


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
    await tick(fake_redis, _factory(db_session), runtime, now=time.time() + 5)


def _recap(conversation, days_ago: float, text: str, session_id=None) -> Memory:
    ended = datetime.now(UTC) - timedelta(days=days_ago)
    return Memory(
        tenant_id=conversation.tenant_id,
        contact_id=conversation.contact_id,
        type=MemoryType.SESSION_RECAP,
        content=f"{ended.strftime('%d/%m/%Y')}: {text}",
        importance=1,
        meta={
            "session_id": str(session_id or uuid.uuid4()),
            "started_at": ended.isoformat(),
            "ended_at": ended.isoformat(),
        },
    )


def _prompt(provider) -> str:
    return "\n".join(str(m.content) for m in provider.calls[-1]["messages"])


async def _first_turn(client, fake_redis, db_session, **overrides):
    headers, channel_id = await _setup(client, **overrides)
    conversation_id = await _send(client, headers, channel_id, "oi")
    provider = FakeLLMProvider([LLMResponse(content="Olá!")], repeat_last=True)
    await _turn(fake_redis, db_session, provider)
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    return headers, channel_id, conversation, provider


async def _second_turn(client, fake_redis, db_session, headers, channel_id, provider) -> str:
    await _send(client, headers, channel_id, "voltei")
    await _turn(fake_redis, db_session, provider)
    return _prompt(provider)


async def test_prompt_holds_three_most_recent_recaps_oldest_first(
    client, fake_redis, db_session
):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session
    )
    for days, name in [(50, "E"), (40, "D"), (30, "C"), (20, "B"), (10, "A")]:
        db_session.add(_recap(conversation, days, f"recap-{name}"))
    db_session.add(_recap(conversation, 200, "recap-ancient"))
    await db_session.commit()

    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)

    assert RECAPS_HEADER in prompt
    assert "may be out of date" in prompt
    assert "recap-ancient" not in prompt
    assert "recap-D" not in prompt and "recap-E" not in prompt
    assert prompt.index("recap-C") < prompt.index("recap-B") < prompt.index("recap-A")


async def test_limit_zero_shows_none(client, fake_redis, db_session):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session, session_recap_limit=0
    )
    db_session.add(_recap(conversation, 1, "recap-x"))
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert RECAPS_HEADER not in prompt and "recap-x" not in prompt


async def test_facts_and_recaps_do_not_push_each_other_out(client, fake_redis, db_session):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session
    )
    for i in range(20):
        db_session.add(
            Memory(
                tenant_id=conversation.tenant_id,
                contact_id=conversation.contact_id,
                type=MemoryType.FACT,
                content=f"fact-{i:02d}",
                importance=3,
            )
        )
    for i in range(3):
        db_session.add(_recap(conversation, 5 + i, f"recap-{i}"))
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    for i in range(20):
        assert f"fact-{i:02d}" in prompt
    for i in range(3):
        assert f"recap-{i}" in prompt
    assert prompt.index("fact-00") < prompt.index(RECAPS_HEADER)


async def test_pending_snapshot_stands_in_until_the_recap_exists(
    client, fake_redis, db_session
):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session
    )
    ended = datetime.now(UTC) - timedelta(days=2)
    row = ConversationSession(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        first_seq=0,
        last_seq=0,
        started_at=ended,
        ended_at=ended,
        ended_by="silence",
        status=SessionStatus.PENDING.value,
        summary_snapshot="snapshot-text",
    )
    db_session.add(row)
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert f"{ended.strftime('%d/%m/%Y')}: snapshot-text" in prompt

    db_session.add(_recap(conversation, 2, "real-recap", session_id=row.id))
    row.status = SessionStatus.DONE.value
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert "real-recap" in prompt and "snapshot-text" not in prompt


async def test_current_sessions_own_recap_is_never_shown(client, fake_redis, db_session):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session
    )
    open_row = (
        await db_session.execute(
            select(ConversationSession).where(
                ConversationSession.conversation_id == conversation.id,
                ConversationSession.status == SessionStatus.OPEN.value,
            )
        )
    ).scalar_one()
    db_session.add(_recap(conversation, 1, "own-recap", session_id=open_row.id))
    db_session.add(_recap(conversation, 3, "other-recap"))
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert "other-recap" in prompt and "own-recap" not in prompt


async def test_behaviour_off_reads_no_recaps(client, fake_redis, db_session):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session, session_memory_enabled=False
    )
    db_session.add(_recap(conversation, 1, "recap-x"))
    await db_session.commit()
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert RECAPS_HEADER not in prompt and "recap-x" not in prompt


async def test_brief_lists_the_recaps(client, fake_redis, db_session):
    _, _, conversation, _ = await _first_turn(client, fake_redis, db_session)
    db_session.add(_recap(conversation, 4, "recap-brief"))
    await db_session.commit()
    agent = await db_session.get(Agent, conversation.agent_id)
    assert agent is not None
    prompt = await handoff_summarizer._build_prompt(db_session, agent, conversation, "x")
    brief_input = str(prompt[-1].content)
    assert "Earlier conversations with the contact" in brief_input
    assert "recap-brief" in brief_input


async def test_memories_api_lists_and_deletes_a_recap(client, fake_redis, db_session):
    headers, channel_id, conversation, provider = await _first_turn(
        client, fake_redis, db_session
    )
    recap = _recap(conversation, 1, "recap-api")
    db_session.add(recap)
    await db_session.commit()
    listed = await client.get(f"/contacts/{conversation.contact_id}/memories", headers=headers)
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json() if m["type"] == "session_recap"] == [str(recap.id)]

    deleted = await client.delete(
        f"/contacts/{conversation.contact_id}/memories/{recap.id}", headers=headers
    )
    assert deleted.status_code == 204
    prompt = await _second_turn(client, fake_redis, db_session, headers, channel_id, provider)
    assert "recap-api" not in prompt
