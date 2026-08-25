"""Rolling conversation summary: triggers at `agent.summary_threshold`, never
touches raw messages, never breaks a turn if the summarizer itself fails.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation
from app.models.message import Message
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


async def _message_count(db_session, conversation_id: uuid.UUID) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )


async def test_summary_set_after_threshold_and_message_count_unchanged(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client, summary_threshold=2)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Olá! Como posso ajudar?")], repeat_last=True)
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([]))
    )

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary is not None
    assert conversation.summary_cursor == 2
    # Two messages (user + assistant reply) went in; the raw table is untouched.
    assert await _message_count(db_session, conversation_id) == 2
    assert provider.call_count == 2  # the reply, then the summarizer


async def test_maybe_summarize_noop_below_threshold(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, summary_threshold=40)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="oi!")])
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([]))
    )

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary is None
    assert conversation.summary_cursor is None
    assert provider.call_count == 1  # no summarizer call below threshold


async def test_maybe_summarize_carries_prior_summary_into_the_prompt_and_advances_cursor(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client, summary_threshold=1)
    await _send(client, headers, channel_id, "primeira mensagem")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Resumo inicial")], repeat_last=True)
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([]))
    )

    conversation_id = await _send(client, headers, channel_id, "segunda mensagem")
    await _drain_jobs(fake_redis)
    provider2 = FakeLLMProvider([LLMResponse(content="Resumo atualizado")], repeat_last=True)
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider2, registry=ToolRegistry([]))
    )

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary == "Resumo atualizado"
    assert conversation.summary_cursor == 4

    # The reply call (0) sees the prior summary in its system prompt...
    assert "## Conversation summary" in provider2.calls[0]["messages"][0].content
    assert "Resumo inicial" in provider2.calls[0]["messages"][0].content
    # ...and the summarizer call (1) is fed the prior summary explicitly too.
    summarizer_prompt = " ".join(m.content or "" for m in provider2.calls[1]["messages"])
    assert "Resumo inicial" in summarizer_prompt


async def test_summary_failure_does_not_break_the_turn(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, summary_threshold=1)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    class FirstOkThenFail:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="oi!")
            raise RuntimeError("summarizer boom")

    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=FirstOkThenFail(), registry=ToolRegistry([])),
    )

    result = await db_session.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    messages = list(result.scalars().all())
    assert any(m.content == "oi!" for m in messages)

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary is None
