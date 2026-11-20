"""`agent.context_message_limit` counts conversation, not tool plumbing (ticket 02).

Counted: USER, HUMAN_OPERATOR, ASSISTANT with a type other than `tool_call`.
The history starts at the limit-th most recent counted message (by `seq`) and
carries every row after it, so a tool call and its results are never split.
"""

import os
import uuid

from app.agents.context import ContextBuilder
from app.agents.runtime import AgentRuntime
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.tools.registry import ToolRegistry


async def _setup(client, **agent_overrides) -> tuple[dict, str, str]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant_response.json()["api_key"]}
    payload = {
        "name": "Bot",
        "instructions": "Você é o atendente da Acme.",
        "model": "gpt-4o-mini",
        **agent_overrides,
    }
    agent = await client.post("/agents", json=payload, headers=headers)
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"], agent.json()["id"]


async def _conversation(client, headers, channel_id) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": "m0"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _add(db_session, conversation, role, content, *, type_="text", call_id=None, raw=None):
    db_session.add(
        Message(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel_id=conversation.channel_id,
            direction=MessageDirection.IN
            if role in (MessageRole.USER, MessageRole.TOOL)
            else MessageDirection.OUT,
            role=role,
            type=type_,
            content=content,
            tool_call_id=call_id,
            raw_payload=raw,
        )
    )
    await db_session.flush()


async def _tool_step(db_session, conversation, name, n_calls):
    """One assistant tool_call row and its `n_calls` results."""
    ids = [f"{name}-{i}" for i in range(n_calls)]
    await _add(
        db_session,
        conversation,
        MessageRole.ASSISTANT,
        f"{name} call",
        type_="tool_call",
        call_id=ids[0],
        raw={"tool_calls": [{"id": i, "name": "get_order", "arguments": {}} for i in ids]},
    )
    for i in ids:
        await _add(
            db_session,
            conversation,
            MessageRole.TOOL,
            f"{i} result",
            type_="tool_result",
            call_id=i,
        )


async def _history(db_session, agent, conversation) -> list[Message]:
    runtime = AgentRuntime(provider=FakeLLMProvider([]), registry=ToolRegistry([]))
    return await runtime._recent_messages(db_session, agent, conversation)


async def _seed_six_with_tools(db_session, conversation):
    """m0 (already there), a1, u2, a3, u4, a5; the last two turns used two tools."""
    await _add(db_session, conversation, MessageRole.ASSISTANT, "a1")
    await _add(db_session, conversation, MessageRole.USER, "u2")
    await _add(db_session, conversation, MessageRole.ASSISTANT, "a3")
    await _add(db_session, conversation, MessageRole.USER, "u4")
    await _tool_step(db_session, conversation, "t1", 2)
    await _add(db_session, conversation, MessageRole.ASSISTANT, "a5")
    await _add(db_session, conversation, MessageRole.USER, "u6")
    await _tool_step(db_session, conversation, "t2", 2)
    await _add(db_session, conversation, MessageRole.ASSISTANT, "a7")


async def test_limit_counts_conversation_messages_and_keeps_tool_rows_between(client, db_session):
    headers, channel_id, agent_id = await _setup(client, context_message_limit=4)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await _seed_six_with_tools(db_session, conversation)

    history = await _history(db_session, agent, conversation)

    # Counted, newest first: a7, u6, a5, u4 -> history starts at u4.
    assert [m.content for m in history] == [
        "u4",
        "t1 call",
        "t1-0 result",
        "t1-1 result",
        "a5",
        "u6",
        "t2 call",
        "t2-0 result",
        "t2-1 result",
        "a7",
    ]
    assert history[0].role == MessageRole.USER


async def test_history_never_starts_with_a_tool_row(client, db_session):
    headers, channel_id, agent_id = await _setup(client, context_message_limit=3)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await _seed_six_with_tools(db_session, conversation)

    history = await _history(db_session, agent, conversation)

    # Counted, newest first: a7, u6, a5 -> starts at a5, not at the t1 rows.
    assert [m.content for m in history][:2] == ["a5", "u6"]
    assert history[0].type not in ("tool_call", "tool_result")
    call_ids = {m.tool_call_id for m in history if m.type == "tool_call"}
    assert {m.tool_call_id for m in history if m.type == "tool_result"} <= {
        "t2-0",
        "t2-1",
    } and "t2-0" in call_ids


async def test_fewer_counted_messages_than_the_limit_sends_every_row(client, db_session):
    headers, channel_id, agent_id = await _setup(client, context_message_limit=20)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await _seed_six_with_tools(db_session, conversation)

    history = await _history(db_session, agent, conversation)

    assert len(history) == 14  # 6 counted... all 14 rows
    assert history[0].content == "m0"


async def test_empty_limit_defaults_to_twenty(client, db_session):
    headers, channel_id, agent_id = await _setup(client)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    for i in range(1, 25):
        await _add(db_session, conversation, MessageRole.USER, f"m{i}")
        await _tool_step(db_session, conversation, f"x{i}", 1)
    agent.context_message_limit = 0

    history = await _history(db_session, agent, conversation)

    counted = [m for m in history if m.type not in ("tool_call", "tool_result")]
    assert len(counted) == 20
    assert history[0].content == "m5"


async def test_operator_messages_count(client, db_session):
    headers, channel_id, agent_id = await _setup(client, context_message_limit=2)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await _add(db_session, conversation, MessageRole.HUMAN_OPERATOR, "op")
    await _tool_step(db_session, conversation, "t", 1)
    await _add(db_session, conversation, MessageRole.USER, "u")

    history = await _history(db_session, agent, conversation)

    assert [m.content for m in history] == ["op", "t call", "t-0 result", "u"]


async def test_builder_does_not_cut_the_history_again(client, db_session):
    headers, channel_id, agent_id = await _setup(client, context_message_limit=4)
    conversation = await db_session.get(
        Conversation, await _conversation(client, headers, channel_id)
    )
    agent = await db_session.get(Agent, uuid.UUID(agent_id))
    await _seed_six_with_tools(db_session, conversation)
    history = await _history(db_session, agent, conversation)

    built = await ContextBuilder().build(agent, conversation, history)

    assert len(built) == 1 + len(history)
    assert built[1].role == "user"
    assert built[1].content == "u4"
