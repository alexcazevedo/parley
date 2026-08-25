"""End-to-end proof of the PRODUCT.md "success demo", over the real pipeline
(ingest -> Redis debounce -> scheduler tick -> AgentRuntime -> tools -> reply)
with the LLM scripted (`FakeLLMProvider`) and the customer's `get_order`
endpoint mocked over HTTP with `respx` — nothing leaves the process, but every
hop the live demo (`scripts/demo.py`) exercises against a real stack is
covered here too:

  1. three rapid messages debounce into a single turn; the agent calls
     `get_order` with `order_id="12345"` and answers, all persisted;
  2. "Quero falar com uma pessoa" -> `handoff_to_human` -> `HUMAN_HANDOFF`;
     the AI goes silent (a new message still persists, but gets no reply);
  3. an operator posts a message, `/resume` brings the AI back, and the next
     message gets a reply again.

Registry, respx and DNS-faking patterns mirror tests/test_http_tools.py and
tests/test_handoff.py.
"""

import os
import socket
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
ORDERS_URL = "https://mock-tools.test/orders"
DEFAULT_HANDOFF_MESSAGE = "Vou te transferir para um atendente humano."


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """`mock-tools.test` resolves to a public address; everything else fails.

    respx intercepts the transport, so this only feeds the SSRF guard — the
    real stack instead allowlists the compose service via
    `TOOL_ALLOWED_HOSTS=mock-tools` (see docker-compose.yml).
    """

    def getaddrinfo(host, port, *args, **kwargs):
        if host != "mock-tools.test":
            raise socket.gaierror(f"no fake DNS entry for {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _registry() -> ToolRegistry:
    # `BuiltinExecutor` (remember, handoff_to_human) before `HttpToolExecutor`
    # (the customer's own tools) — the same order app.tools.registry's
    # `default_executors()` uses in production.
    return ToolRegistry([BuiltinExecutor(), HttpToolExecutor()])


ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


async def _setup(client) -> tuple[dict, str]:
    """Tenant + get_order tool + agent (memory/handoff on, get_order wired) +
    webchat channel — the same shape `scripts/demo.py` builds against a live
    stack.
    """
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}

    tool_resp = await client.post(
        "/tools",
        json={
            "name": "get_order",
            "description": "Consulta o status de um pedido pelo id",
            "input_schema": ORDER_SCHEMA,
            "endpoint": ORDERS_URL,
            "method": "GET",
            "timeout_seconds": 5,
        },
        headers=headers,
    )
    assert tool_resp.status_code == 201, tool_resp.text

    agent_resp = await client.post(
        "/agents",
        json={
            "name": "Atendente Acme",
            "instructions": "Você é o atendente virtual da Acme.",
            "model": "gpt-4o-mini",
            "tool_names": ["get_order"],
            "memory_enabled": True,
            "handoff_enabled": True,
        },
        headers=headers,
    )
    assert agent_resp.status_code == 201, agent_resp.text

    channel_resp = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web Demo", "agent_id": agent_resp.json()["id"]},
        headers=headers,
    )
    assert channel_resp.status_code == 201, channel_resp.text
    return headers, channel_resp.json()["id"]


async def _send(client, headers, channel_id, text, user_id="demo-user") -> uuid.UUID:
    response = await client.post(
        "/dev/messages",
        json={"channel_id": channel_id, "user_id": user_id, "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _run_turn(fake_redis, db_session, provider) -> None:
    runtime = AgentRuntime(provider=provider, registry=_registry())
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _messages(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


class MustNotBeCalledProvider:
    """Fails the test loudly if invoked — proves the AI stays silent while
    a conversation is handed off (see tests/test_handoff.py).
    """

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise AssertionError("the LLM must not be called while handed off")


async def test_success_demo_end_to_end(client, fake_redis, db_session):
    headers, channel_id = await _setup(client)

    # --- 1. three rapid messages debounce into one turn; get_order(12345),
    #        final answer persisted ------------------------------------------
    texts = ["Oi", "Queria saber onde está", "meu pedido 12345"]
    conversation_id = None
    for text in texts:
        conversation_id = await _send(client, headers, channel_id, text)
    assert conversation_id is not None
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="get_order", arguments={"order_id": "12345"})
                ]
            ),
            LLMResponse(content="Seu pedido 12345 já foi enviado, chega amanhã."),
        ]
    )
    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json={"status": "shipped", "eta": "amanhã"})
        )
        await _run_turn(fake_redis, db_session, provider)

    assert route.called
    assert route.calls.last.request.url.params["order_id"] == "12345"
    assert provider.call_count == 2

    messages = await _messages(db_session, conversation_id)
    assert [m.role for m in messages[:3]] == [MessageRole.USER] * 3
    reply = messages[-1]
    assert reply.role == MessageRole.ASSISTANT
    assert "12345" in reply.content

    run = (
        (
            await db_session.execute(
                select(AgentRun).where(AgentRun.conversation_id == conversation_id)
            )
        )
        .scalars()
        .one()
    )
    assert run.status == AgentRunStatus.SUCCESS
    assert run.tool_calls == 1

    execution = (
        (
            await db_session.execute(
                select(ToolExecution).where(ToolExecution.agent_run_id == run.id)
            )
        )
        .scalars()
        .one()
    )
    assert execution.status == ToolExecutionStatus.OK
    assert execution.tool_name == "get_order"
    assert execution.arguments == {"order_id": "12345"}

    # --- 2. "Quero falar com uma pessoa" -> handoff_to_human -> HUMAN_HANDOFF,
    #        then the AI stays silent on the next message ---------------------
    await _send(client, headers, channel_id, "Quero falar com uma pessoa")
    await _drain_jobs(fake_redis)

    handoff_provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_2",
                        name="handoff_to_human",
                        arguments={"reason": "usuário pediu atendente humano"},
                    )
                ]
            )
        ]
    )
    await _run_turn(fake_redis, db_session, handoff_provider)
    assert handoff_provider.call_count == 1

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.state == ConversationState.HUMAN_HANDOFF

    handoffs = (
        (
            await db_session.execute(
                select(Handoff).where(Handoff.conversation_id == conversation_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(handoffs) == 1
    assert handoffs[0].requested_by == HandoffRequestedBy.AGENT
    assert handoffs[0].resolved_at is None

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == DEFAULT_HANDOFF_MESSAGE

    # A new message still persists...
    await _send(client, headers, channel_id, "alguém aí?")
    await _drain_jobs(fake_redis)
    messages_after = await _messages(db_session, conversation_id)
    assert messages_after[-1].content == "alguém aí?"

    # ...but the AI never runs again while handed off.
    await _run_turn(fake_redis, db_session, MustNotBeCalledProvider())
    messages_still = await _messages(db_session, conversation_id)
    assert messages_still[-1].content == "alguém aí?"

    # --- 3. operator posts, /resume, next message gets a reply again --------
    operator_resp = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "Oi, aqui é o atendente. Como posso ajudar?"},
        headers=headers,
    )
    assert operator_resp.status_code == 201, operator_resp.text
    assert operator_resp.json()["role"] == "human_operator"

    resume_resp = await client.post(f"/conversations/{conversation_id}/resume", headers=headers)
    assert resume_resp.status_code == 200, resume_resp.text
    assert resume_resp.json()["state"] == "ACTIVE_AI"

    resolved_handoffs = (
        (
            await db_session.execute(
                select(Handoff).where(Handoff.conversation_id == conversation_id)
            )
        )
        .scalars()
        .all()
    )
    assert all(h.resolved_at is not None for h in resolved_handoffs)

    await _send(client, headers, channel_id, "Obrigado!")
    await _drain_jobs(fake_redis)
    final_provider = FakeLLMProvider([LLMResponse(content="De nada! Qualquer coisa é só chamar.")])
    await _run_turn(fake_redis, db_session, final_provider)
    assert final_provider.call_count == 1

    final_messages = await _messages(db_session, conversation_id)
    assert final_messages[-1].role == MessageRole.ASSISTANT
    assert final_messages[-1].content == "De nada! Qualquer coisa é só chamar."

    # And the REST surface (`?include_tools=false` default) shows the same
    # clean transcript a client polling for replies would see.
    listed = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[-1]["content"] == "De nada! Qualquer coisa é só chamar."
    assert "tool" not in [m["role"] for m in listed.json()]
