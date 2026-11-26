"""Success demo for agent boundaries, end to end through the real pipeline:
API -> ingest -> scheduler tick -> AgentRuntime with a scripted FakeLLMProvider.
The reply check is simply the next scripted response after the agent's reply.
"""

import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.agents.boundaries import DEFAULT_REFUSAL_MESSAGE, load_floor
from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

INSTRUCTIONS = "Você é a recepcionista da clínica."
REFUSAL = "Só posso falar sobre consultas da clínica."
SCOPE = "agendamento de consultas da clínica"
NEVER = ["dar conselho médico", "prometer preços"]
BOUNDED = {
    "boundary_scope": SCOPE,
    "boundary_never": NEVER,
    "boundary_refusal_message": REFUSAL,
}
OFF_TOPIC = "Aqui vai um programa em Python que resolve isso."


@pytest.fixture
def floor(tmp_path, monkeypatch):
    """Sets an operator floor for one test and resets it afterwards."""
    path = tmp_path / "floor.json"
    path.write_text(json.dumps({"never": ["falar de política"]}))
    monkeypatch.setenv("BOUNDARY_FLOOR_FILE", str(path))
    get_settings.cache_clear()
    load_floor.cache_clear()
    yield
    monkeypatch.delenv("BOUNDARY_FLOOR_FILE")
    get_settings.cache_clear()
    load_floor.cache_clear()


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _turn(client, fake_redis, db_session, provider, **agent) -> uuid.UUID:
    tenant = await client.post(
        "/tenants", json={"name": "Clinic"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    created = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": INSTRUCTIONS, "model": "gpt-4o-mini", **agent},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": created.json()["id"]},
        headers=headers,
    )
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "oi"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation_id = uuid.UUID(sent.json()["conversation_id"])

    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry())
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    return conversation_id


async def _outbound(db_session, conversation_id) -> list[str]:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role != MessageRole.USER)
        .order_by(Message.seq)
    )
    return [m.content for m in result.scalars().all()]


async def _run(db_session, conversation_id) -> AgentRun:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    runs = list(result.scalars().all())
    assert len(runs) == 1
    return runs[0]


async def test_out_of_bounds_reply_is_replaced_by_refusal(client, fake_redis, db_session):
    provider = FakeLLMProvider(
        [LLMResponse(content=OFF_TOPIC), LLMResponse(content="BLOCK: off topic")]
    )
    conv = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    outbound = await _outbound(db_session, conv)
    assert outbound == [REFUSAL]
    assert all(OFF_TOPIC not in text for text in outbound)
    run = await _run(db_session, conv)
    assert run.boundary_reason == "off topic"
    assert run.blocked_text == OFF_TOPIC
    assert run.output_text == REFUSAL


async def test_in_bounds_reply_is_delivered_unchanged(client, fake_redis, db_session):
    reply = "Temos horário amanhã às 10h."
    provider = FakeLLMProvider([LLMResponse(content=reply), LLMResponse(content="ALLOW")])
    conv = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert await _outbound(db_session, conv) == [reply]
    run = await _run(db_session, conv)
    assert run.boundary_reason is None
    assert run.output_text == reply


async def test_floor_blocks_agent_without_boundaries(client, fake_redis, db_session, floor):
    provider = FakeLLMProvider(
        [LLMResponse(content="Sobre política, acho que..."), LLMResponse(content="BLOCK: politics")]
    )
    conv = await _turn(
        client, fake_redis, db_session, provider, boundary_check_enabled=False
    )

    assert provider.call_count == 2
    assert await _outbound(db_session, conv) == [DEFAULT_REFUSAL_MESSAGE]
    run = await _run(db_session, conv)
    assert run.boundary_reason == "politics"


async def test_no_floor_no_boundaries_makes_no_extra_call(client, fake_redis, db_session):
    provider = FakeLLMProvider([LLMResponse(content="Olá!")])
    conv = await _turn(client, fake_redis, db_session, provider)

    assert provider.call_count == 1
    assert provider.calls[0]["messages"][0].content == INSTRUCTIONS
    assert await _outbound(db_session, conv) == ["Olá!"]


async def test_system_prompt_contains_scope_and_never_items(client, fake_redis, db_session):
    provider = FakeLLMProvider([LLMResponse(content="Olá!"), LLMResponse(content="ALLOW")])
    await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    system = provider.calls[0]["messages"][0].content
    assert INSTRUCTIONS in system
    assert SCOPE in system
    for item in NEVER:
        assert item in system
