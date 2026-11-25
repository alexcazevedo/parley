"""Staff messages on an external channel: they pause the agent, the project is
told when the agent goes off or on (`conversation.agent`, `conversation.handoff`
events), and resume hands the conversation back.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.runtime import AgentRuntime
from app.api.deps import get_db
from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.main import app
from app.models.channel_event import ChannelEvent
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole
from tests.test_external_channel import (
    REPLY,
    _events,
    _inbound,
    _post,
    _run_turn,
    _setup,
)


async def _staff(client, headers, channel_id, text="Olá, aqui é a loja.", **extra):
    body = {
        "channel_id": channel_id,
        "user_id": "cust-1",
        "message": text,
        "sender": "staff",
        **extra,
    }
    return await client.post("/channels/external/messages", json=body, headers=headers)


async def _types(client, headers, channel_id) -> list[tuple[str, dict]]:
    feed = (await _events(client, headers, channel_id)).json()
    return [(e["type"], e["data"]) for e in feed]


async def _state(db_session, conversation_id) -> ConversationState:
    db_session.expire_all()
    conversation = await db_session.get(Conversation, conversation_id)
    return conversation.state


async def test_staff_message_pauses_the_agent_and_resume_restores_it(client, db_session):
    headers, channel_id, _ = await _setup(client)
    first = await _post(client, headers, channel_id, "oi")
    conversation_id = uuid.UUID(first.json()["conversation_id"])

    response = await _staff(client, headers, channel_id, staff_name="Marina")
    assert response.status_code == 201, response.text
    assert response.json()["conversation_id"] == str(conversation_id)
    assert response.json()["duplicate"] is False
    assert await _state(db_session, conversation_id) == ConversationState.HUMAN_HANDOFF

    stored = (
        await db_session.execute(
            select(Message).where(Message.id == uuid.UUID(response.json()["message_id"]))
        )
    ).scalar_one()
    assert stored.direction == MessageDirection.OUT
    assert stored.role == MessageRole.HUMAN_OPERATOR
    assert stored.raw_payload["staff_name"] == "Marina"

    assert await _types(client, headers, channel_id) == [
        (
            "conversation.handoff",
            {"reason": "staff message", "requested_by": "operator", "brief": None},
        ),
        ("conversation.agent", {"active": False, "cause": "staff_message"}),
    ]

    # The agent stays quiet on the next customer message.
    await _post(client, headers, channel_id, "e aí?")
    assert await _run_turn(db_session, conversation_id) is None
    assert len(await _types(client, headers, channel_id)) == 2

    resumed = await client.post(f"/conversations/{conversation_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert (await _types(client, headers, channel_id))[-1] == (
        "conversation.agent",
        {"active": True, "cause": "resumed"},
    )

    await _post(client, headers, channel_id, "voltei")
    reply = await _run_turn(db_session, conversation_id)
    assert isinstance(reply, Message) and reply.content == REPLY
    assert (await _types(client, headers, channel_id))[-1][0] == "message.reply"


async def test_second_staff_message_records_no_second_pause(client, db_session):
    headers, channel_id, _ = await _setup(client)
    await _post(client, headers, channel_id, "oi")
    await _staff(client, headers, channel_id)
    again = await _staff(client, headers, channel_id, "Mais uma coisa.")
    assert again.status_code == 201
    pauses = [t for t in await _types(client, headers, channel_id) if t[0] == "conversation.agent"]
    assert len(pauses) == 1


async def test_staff_message_dedupes_by_message_id(client, db_session):
    headers, channel_id, _ = await _setup(client)
    one = await _staff(client, headers, channel_id, message_id="s-1")
    two = await _staff(client, headers, channel_id, message_id="s-1")
    assert two.json()["duplicate"] is True
    assert two.json()["message_id"] == one.json()["message_id"]
    assert len(await _types(client, headers, channel_id)) == 2


async def test_staff_message_without_a_conversation_creates_it_handed_off(client, db_session):
    headers, channel_id, _ = await _setup(client)
    response = await _staff(client, headers, channel_id)
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    assert await _state(db_session, conversation_id) == ConversationState.HUMAN_HANDOFF
    assert await _inbound(db_session, conversation_id) == []
    assert [t for t, _ in await _types(client, headers, channel_id)] == [
        "conversation.handoff",
        "conversation.agent",
    ]


async def test_staff_name_is_bounded(client, db_session):
    headers, channel_id, _ = await _setup(client)
    response = await _staff(client, headers, channel_id, staff_name="x" * 121)
    assert response.status_code == 422


async def test_turn_running_when_staff_writes_sends_no_reply(client, db_session):
    headers, channel_id, _ = await _setup(client)
    first = await _post(client, headers, channel_id, "oi")
    conversation_id = uuid.UUID(first.json()["conversation_id"])
    messages = await _inbound(db_session, conversation_id)

    class StaffWritesDuringTurn:
        async def run(self, conversation, input_text):
            # The staff request has its own session, as in production.
            async def own_session():
                async with async_sessionmaker(
                    bind=db_session.bind, expire_on_commit=False
                )() as other:
                    yield other

            previous = app.dependency_overrides[get_db]
            app.dependency_overrides[get_db] = own_session
            try:
                response = await _staff(client, headers, channel_id)
            finally:
                app.dependency_overrides[get_db] = previous
            assert response.status_code == 201, response.text
            return "resposta velha"

    result = await process_turn(db_session, conversation_id, messages, StaffWritesDuringTurn())
    assert result is None
    db_session.expire_all()
    replies = (
        (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.role == MessageRole.ASSISTANT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert replies == []
    assert "message.reply" not in [t for t, _ in await _types(client, headers, channel_id)]
    assert await _state(db_session, conversation_id) == ConversationState.HUMAN_HANDOFF


async def test_agent_handoff_records_handoff_and_agent_events(
    client, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    agent_id = (await client.get("/agents", headers=headers)).json()[0]["id"]
    patched = await client.patch(
        f"/agents/{agent_id}", json={"handoff_enabled": True}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    first = await _post(client, headers, channel_id, "quero falar com uma pessoa")
    conversation_id = uuid.UUID(first.json()["conversation_id"])

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="handoff_to_human", arguments={"reason": "pedido"})
                ]
            ),
            LLMResponse(content="ok"),
        ]
    )
    messages = await _inbound(db_session, conversation_id)
    await process_turn(db_session, conversation_id, messages, AgentRuntime(provider=provider))

    types = await _types(client, headers, channel_id)
    assert types[0] == (
        "conversation.handoff",
        {"reason": "pedido", "requested_by": "agent", "brief": fake_handoff_brief},
    )
    assert types[1] == ("conversation.agent", {"active": False, "cause": "agent_handoff"})
    assert types[2][0] == "message.reply"


async def test_operator_handoff_brief_and_close_events(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    first = await _post(client, headers, channel_id, "oi")
    conversation_id = first.json()["conversation_id"]

    handed = await client.post(
        f"/conversations/{conversation_id}/handoff", json={"reason": "vip"}, headers=headers
    )
    assert handed.status_code == 200, handed.text
    closed = await client.post(f"/conversations/{conversation_id}/close", headers=headers)
    assert closed.status_code == 200

    assert await _types(client, headers, channel_id) == [
        ("conversation.handoff", {"reason": "vip", "requested_by": "operator", "brief": None}),
        ("conversation.agent", {"active": False, "cause": "operator"}),
        ("conversation.handoff_brief", {"brief": fake_handoff_brief}),
        ("conversation.agent", {"active": False, "cause": "closed"}),
    ]


async def test_webchat_conversation_records_nothing_on_handoff(client, db_session):
    headers, channel_id, _ = await _setup(client, channel_type="webchat")
    first = await _post(client, headers, channel_id, "oi", path="webchat")
    conversation_id = first.json()["conversation_id"]
    resumed = await client.post(f"/conversations/{conversation_id}/close", headers=headers)
    assert resumed.status_code == 200
    count = await db_session.scalar(
        select(func.count())
        .select_from(ChannelEvent)
        .where(ChannelEvent.conversation_id == uuid.UUID(conversation_id))
    )
    assert count == 0
    assert turn_module is not None
