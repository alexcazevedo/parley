"""An operator's handoff, resume or close wins over a running turn (E4.2).

The operator acts through the real endpoint functions of
`app/api/conversations.py`, in a session of its own (from `test_engine`), while
the turn is paused: inside the pacing sleep between humanizer bubbles, or
inside the model call. The turn must then send nothing more, store nothing
more, and its own `handoff_to_human` call must not overwrite the operator's
newer state. The operator handoff endpoint commits the state change before the
brief's model call starts.
"""

import os
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.api import conversations as conversations_api
from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tenant import Tenant
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry

HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."
MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")
AI_REPLY = "Claro, posso ajudar com isso."
BRIEF_TEXT = "Cliente pediu atendimento humano."


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        self.sent.append(message.content)


class TextRuntime:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        return self.reply


class OperatorDuringModelCall:
    """Model double: the operator acts during the first call, then scripted replies."""

    def __init__(self, operator_action, responses: list[LLMResponse]) -> None:
        self.operator_action = operator_action
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.calls += 1
        if self.calls == 1:
            await self.operator_action()
        return self.responses.pop(0)


async def _prepare(client, db_session, monkeypatch, *, humanize: bool):
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    created_agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Você é o atendente da Acme.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert created_agent.status_code == 201, created_agent.text
    agent = await db_session.get(Agent, uuid.UUID(created_agent.json()["id"]))
    agent.handoff_message = HANDOFF_MESSAGE
    await db_session.commit()
    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": created_agent.json()["id"],
            "config": {"humanize": True} if humanize else {},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "oi"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    conversation_id = uuid.UUID(response.json()["conversation_id"])

    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN)
        .order_by(Message.seq)
    )
    messages = list(result.scalars().all())

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)]),
    )
    return conversation_id, messages, adapter, headers


async def _operator(engine, conversation_id: uuid.UUID, action: str) -> None:
    """The operator's request, through the endpoint function, in its own session."""
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as other:
        conversation = await other.get(Conversation, conversation_id)
        tenant = await other.get(Tenant, conversation.tenant_id)
        if action == "handoff":
            await conversations_api.handoff_conversation(
                conversation_id,
                conversations_api.HandoffCreate(reason="operador assumiu"),
                tenant=tenant,
                db=other,
            )
        elif action == "close":
            await conversations_api.close_conversation(conversation_id, tenant=tenant, db=other)
        else:  # pragma: no cover - test helper misuse
            raise AssertionError(action)


async def _stored_replies(engine, conversation_id) -> list[str]:
    async with async_sessionmaker(bind=engine)() as other:
        result = await other.execute(
            select(Message.content)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUT,
                Message.role == MessageRole.ASSISTANT,
                Message.type == "text",
            )
            .order_by(Message.seq)
        )
        return list(result.scalars().all())


async def _committed_state(engine, conversation_id) -> ConversationState:
    async with async_sessionmaker(bind=engine)() as other:
        conversation = await other.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation.state


async def _handoff_count(engine, conversation_id) -> int:
    async with async_sessionmaker(bind=engine)() as other:
        return (
            await other.execute(
                select(func.count())
                .select_from(Handoff)
                .where(Handoff.conversation_id == conversation_id)
            )
        ).scalar_one()


def _stops(logs: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in entry.items() if key not in ("event", "log_level")}
        for entry in logs
        if entry["event"] == "turn.stopped_state_changed"
    ]


async def test_operator_handoff_during_pacing_pause_drops_the_remaining_bubbles(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=True
    )
    sleeps = 0

    async def pausing_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:  # the pause before bubble 2
            await _operator(test_engine, conversation_id, "handoff")

    monkeypatch.setattr(turn_module.asyncio, "sleep", pausing_sleep)

    with capture_logs() as logs:
        result = await process_turn(
            db_session, conversation_id, messages, TextRuntime(MULTI_PART_REPLY)
        )

    assert isinstance(result, Message)
    assert result.content == PARTS[0]
    assert adapter.sent == [PARTS[0]]
    assert await _stored_replies(test_engine, conversation_id) == [PARTS[0]]
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )
    assert _stops(logs) == [
        {
            "conversation_id": str(conversation_id),
            "state": str(ConversationState.HUMAN_HANDOFF),
            "delivered_parts": 1,
        }
    ]


async def test_operator_handoff_during_model_call_sends_and_stores_nothing(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )
    async def operator_handoff() -> None:
        await _operator(test_engine, conversation_id, "handoff")

    provider = OperatorDuringModelCall(operator_handoff, [LLMResponse(content=AI_REPLY)])
    runtime = AgentRuntime(provider=provider)

    with capture_logs() as logs:
        result = await process_turn(db_session, conversation_id, messages, runtime)

    assert result is None
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )
    assert [stop["delivered_parts"] for stop in _stops(logs)] == [0]


async def test_operator_close_during_model_call_sends_and_stores_nothing(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=True
    )

    async def operator_close() -> None:
        await _operator(test_engine, conversation_id, "close")

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", no_sleep)
    provider = OperatorDuringModelCall(operator_close, [LLMResponse(content=MULTI_PART_REPLY)])
    runtime = AgentRuntime(provider=provider)

    result = await process_turn(db_session, conversation_id, messages, runtime)

    assert result is None
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []
    assert await _committed_state(test_engine, conversation_id) == ConversationState.CLOSED


async def test_tool_handoff_after_operator_close_does_not_reopen(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )

    async def operator_close() -> None:
        await _operator(test_engine, conversation_id, "close")

    provider = OperatorDuringModelCall(
        operator_close,
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="handoff_to_human",
                        arguments={"reason": "pediu atendimento humano"},
                    )
                ]
            ),
            # The refused tool call goes back to the model, which answers.
            LLMResponse(content=AI_REPLY),
        ],
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    result = await process_turn(db_session, conversation_id, messages, runtime)

    assert result is None
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []
    assert await _committed_state(test_engine, conversation_id) == ConversationState.CLOSED
    assert await _handoff_count(test_engine, conversation_id) == 0


async def test_operator_handoff_endpoint_commits_state_before_the_brief(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, _, _, headers = await _prepare(client, db_session, monkeypatch, humanize=False)
    seen_at_brief: list[ConversationState] = []

    class BriefProvider:
        async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
            seen_at_brief.append(await _committed_state(test_engine, conversation_id))
            return LLMResponse(content=BRIEF_TEXT)

    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: BriefProvider()
    )

    response = await client.post(
        f"/conversations/{conversation_id}/handoff", json={"reason": "x"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == ConversationState.HUMAN_HANDOFF
    assert seen_at_brief == [ConversationState.HUMAN_HANDOFF]
    single = await client.get(f"/conversations/{conversation_id}", headers=headers)
    assert single.json()["latest_handoff"]["summary"] == BRIEF_TEXT


async def test_operator_handoff_endpoint_keeps_the_handoff_when_the_brief_fails(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, _, _, headers = await _prepare(client, db_session, monkeypatch, humanize=False)
    states_at_brief: list[ConversationState] = []

    class FailingProvider:
        async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
            states_at_brief.append(await _committed_state(test_engine, conversation_id))
            raise RuntimeError("provider down")

    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: FailingProvider()
    )

    response = await client.post(
        f"/conversations/{conversation_id}/handoff", json={"reason": "x"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert states_at_brief == [ConversationState.HUMAN_HANDOFF]
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )
    assert await _handoff_count(test_engine, conversation_id) == 1
