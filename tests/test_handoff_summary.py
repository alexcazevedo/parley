"""The warm-handoff operator brief: `app.handoff.summarizer.handoff_with_summary`
persists a best-effort LLM-written `Handoff.summary` for both the
`handoff_to_human` tool path and the operator REST endpoint, and never lets a
broken summarizer block the escalation itself.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
BRIEF_TEXT = "Contact wants a refund; bot could not process it; user is frustrated."


class RaisingProvider:
    """Always fails — proves the summarizer's LLM failure path is harmless."""

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise RuntimeError("llm is down")


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


async def _handoffs(db_session, conversation_id: uuid.UUID) -> list[Handoff]:
    result = await db_session.execute(
        select(Handoff).where(Handoff.conversation_id == conversation_id)
    )
    return list(result.scalars().all())


async def test_agent_tool_handoff_persists_summary(client, fake_redis, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero falar com uma pessoa")
    await _drain_jobs(fake_redis)

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )

    tool_provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="handoff_to_human",
                        arguments={"reason": "usuário pediu atendente humano"},
                    )
                ]
            )
        ]
    )
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=tool_provider, registry=ToolRegistry([BuiltinExecutor()])),
    )

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.state == ConversationState.HUMAN_HANDOFF

    handoffs = await _handoffs(db_session, conversation_id)
    assert len(handoffs) == 1
    assert handoffs[0].summary == BRIEF_TEXT
    assert summary_provider.call_count == 1


async def test_operator_endpoint_handoff_persists_summary(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )

    resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "cliente irritado"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "HUMAN_HANDOFF"

    handoffs = await _handoffs(db_session, conversation_id)
    assert len(handoffs) == 1
    assert handoffs[0].summary == BRIEF_TEXT


async def test_provider_failure_still_completes_handoff_with_null_summary(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: RaisingProvider()
    )

    resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "cliente irritado"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "HUMAN_HANDOFF"

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.state == ConversationState.HUMAN_HANDOFF

    handoffs = await _handoffs(db_session, conversation_id)
    assert len(handoffs) == 1
    assert handoffs[0].summary is None


async def test_summarizer_prompt_carries_the_handoff_reason(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )

    reason = "cliente quer cancelar a assinatura e está muito bravo"
    resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": reason},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    assert summary_provider.call_count == 1
    prompt_text = " ".join(m.content or "" for m in summary_provider.calls[0]["messages"])
    assert reason in prompt_text

    handoffs = await _handoffs(db_session, conversation_id)
    assert handoffs[0].summary == BRIEF_TEXT
