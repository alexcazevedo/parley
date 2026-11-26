"""Human handoff: the `handoff_to_human` built-in tool end-to-end, the
handoff/resume/close REST transitions, the operator message endpoint, and
`?include_tools=false` filtering.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.base import ToolContext
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
DEFAULT_HANDOFF_MESSAGE = "Vou te transferir para um atendente humano."


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


async def _messages(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


class MustNotBeCalledProvider:
    """Fails the test loudly if the runtime ever invokes it — used to prove
    the AI stays silent once a conversation is out of `ACTIVE_AI`.
    """

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise AssertionError("the LLM must not be called while handed off")


async def test_handoff_tool_flips_state_sends_message_and_silences_ai(
    client, fake_redis, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "quero falar com uma pessoa")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
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
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )

    # Only one LLM call: the loop stopped instead of asking the model again.
    assert provider.call_count == 1

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
    assert handoffs[0].reason == "usuário pediu atendente humano"
    assert handoffs[0].resolved_at is None

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == DEFAULT_HANDOFF_MESSAGE

    # A new user message still gets persisted...
    await _send(client, headers, channel_id, "alguém aí?")
    await _drain_jobs(fake_redis)
    messages_after = await _messages(db_session, conversation_id)
    assert messages_after[-1].content == "alguém aí?"

    # ...but the AI never runs again while handed off.
    await _run_turn(fake_redis, db_session, MustNotBeCalledProvider())
    messages_final = await _messages(db_session, conversation_id)
    assert messages_final[-1].content == "alguém aí?"


async def test_operator_message_then_resume_brings_ai_back(
    client, fake_redis, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    handoff_resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "cliente irritado"},
        headers=headers,
    )
    assert handoff_resp.status_code == 200, handoff_resp.text
    assert handoff_resp.json()["state"] == "HUMAN_HANDOFF"

    posted = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "Oi, aqui é o atendente, como posso ajudar?"},
        headers=headers,
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["role"] == "human_operator"
    assert posted.json()["direction"] == "out"

    listed = await client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()[-1]["content"] == "Oi, aqui é o atendente, como posso ajudar?"

    resumed = await client.post(f"/conversations/{conversation_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "ACTIVE_AI"

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
    assert handoffs[0].resolved_at is not None

    await _send(client, headers, channel_id, "obrigado!")
    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([LLMResponse(content="De nada!")])
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )
    assert provider.call_count == 1
    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == "De nada!"


async def test_illegal_transitions_return_409(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    # Can't resume a conversation that was never handed off.
    resp = await client.post(f"/conversations/{conversation_id}/resume", headers=headers)
    assert resp.status_code == 409

    close_resp = await client.post(f"/conversations/{conversation_id}/close", headers=headers)
    assert close_resp.status_code == 200
    assert close_resp.json()["state"] == "CLOSED"

    # Can't hand off a closed conversation.
    resp = await client.post(
        f"/conversations/{conversation_id}/handoff", json={"reason": "x"}, headers=headers
    )
    assert resp.status_code == 409


async def test_list_conversations_filters_by_state(
    client, fake_redis, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    active_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    # A different `user_id` so the two sends land on two conversations, not
    # one reused thread — see `test_ingest.py::test_two_users_get_two_conversations`.
    other_resp = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-2", "message": "quero um humano"},
        headers=headers,
    )
    assert other_resp.status_code == 201, other_resp.text
    handed_off_id = uuid.UUID(other_resp.json()["conversation_id"])
    await _drain_jobs(fake_redis)
    handoff_resp = await client.post(
        f"/conversations/{handed_off_id}/handoff", json={"reason": "x"}, headers=headers
    )
    assert handoff_resp.status_code == 200

    resp = await client.get(
        "/conversations", params={"state": "HUMAN_HANDOFF"}, headers=headers
    )
    assert resp.status_code == 200
    ids = {c["id"] for c in resp.json()}
    assert ids == {str(handed_off_id)}
    assert str(active_id) not in ids


async def test_include_tools_filter_hides_tool_call_and_result_rows(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "faz mágica")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
        [
            LLMResponse(tool_calls=[ToolCall(id="call_1", name="do_magic", arguments={})]),
            LLMResponse(content="Não consigo fazer isso."),
        ]
    )
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([]))
    )

    default_resp = await client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    )
    assert default_resp.status_code == 200
    default_roles = [m["role"] for m in default_resp.json()]
    assert "tool" not in default_roles
    assert all(m["type"] != "tool_call" for m in default_resp.json())

    with_tools_resp = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"include_tools": "true"},
        headers=headers,
    )
    with_tools_roles = [m["role"] for m in with_tools_resp.json()]
    assert "tool" in with_tools_roles
    assert any(m["type"] == "tool_call" for m in with_tools_resp.json())


async def test_handoff_disabled_agent_does_not_advertise_or_execute(
    client, fake_redis, db_session
):
    headers, channel_id, agent_id = await _setup(client, handoff_enabled=False)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="oi!")])
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )
    tool_names = [spec.name for spec in provider.calls[0]["tools"]]
    assert "handoff_to_human" not in tool_names

    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    ctx = ToolContext(
        session=db_session,
        tenant_id=agent.tenant_id,
        conversation_id=conversation_id,
        contact_id=uuid.uuid4(),
        agent=agent,
        agent_run_id=uuid.uuid4(),
    )
    result = await BuiltinExecutor().execute("handoff_to_human", {"reason": "x"}, ctx)
    assert result.ok is False
    assert "disabled" in result.error
