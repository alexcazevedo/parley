"""A handoff survives barge-in, and a superseded turn marks only its own run.

E4.1: when the turn's own `handoff_to_human` tool call hands the conversation
to a human, a newer customer message must not cancel the handoff message — not
at checkpoint 1 (before delivery) and not between humanizer bubbles. The
message goes out whole, is stored, and the turn returns it.

E4.6: a superseded turn marks the `AgentRun` it created, never another run of
the same conversation; a turn that created no run marks nothing.

`process_turn` is driven directly with `AgentRuntime` and a scripted provider,
plus a `supersede_check` that reports a newer message.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import Superseded, process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry

HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."
MULTI_PART_HANDOFF = (
    "Entendi, vou chamar uma pessoa do time.\n\n"
    "Ela já recebeu o resumo da nossa conversa.\n\n"
    "Em instantes alguém te responde por aqui."
)
HANDOFF_PARTS = MULTI_PART_HANDOFF.split("\n\n")
BRIEF_TEXT = "Cliente pediu atendimento humano."


class ScriptedCheck:
    """`supersede_check` double: answers from a list, then `default`."""

    def __init__(self, answers: list[bool] | None = None, *, default: bool = True) -> None:
        self.answers = list(answers or [])
        self.default = default
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.answers.pop(0) if self.answers else self.default


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        self.sent.append(message.content)


class PlainRuntime:
    """Text-in/text-out runtime: writes no `AgentRun`."""

    async def run(self, conversation, input_text: str) -> str:
        return "Claro, posso ajudar."


async def _prepare(client, db_session, monkeypatch, *, humanize: bool, handoff_message: str):
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
    agent.handoff_message = handoff_message
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
        json={
            "channel_id": channel.json()["id"],
            "user_id": "user-1",
            "message": "quero uma pessoa",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    conversation_id = uuid.UUID(response.json()["conversation_id"])

    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN)
        .order_by(Message.created_at)
    )
    messages = list(result.scalars().all())

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)]),
    )
    return conversation_id, messages, adapter


def _handoff_runtime() -> AgentRuntime:
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="handoff_to_human",
                        arguments={"reason": "pediu atendimento humano"},
                    )
                ]
            )
        ]
    )
    return AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))


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
            .order_by(Message.created_at)
        )
        return list(result.scalars().all())


async def _committed_state(engine, conversation_id) -> ConversationState:
    async with async_sessionmaker(bind=engine)() as other:
        conversation = await other.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation.state


async def _run_statuses(engine, conversation_id) -> dict[uuid.UUID, AgentRunStatus]:
    async with async_sessionmaker(bind=engine)() as other:
        result = await other.execute(
            select(AgentRun.id, AgentRun.status).where(AgentRun.conversation_id == conversation_id)
        )
        return {row.id: row.status for row in result.all()}


async def _other_turns_run(db_session, conversation_id) -> uuid.UUID:
    """A run of another turn of this conversation, stamped later than this turn's."""
    conversation = await db_session.get(Conversation, conversation_id)
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation_id,
        status=AgentRunStatus.SUCCESS,
        input_text="mensagem anterior",
        created_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(run)
    await db_session.commit()
    return run.id


def _forbid_summary(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def recording_summarize(session, conversation, runtime) -> None:
        calls.append(str(conversation.id))

    monkeypatch.setattr(turn_module, "_maybe_summarize", recording_summarize)
    return calls


# --- E4.1 -----------------------------------------------------------------------


async def test_tool_handoff_is_delivered_despite_a_newer_message(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, humanize=False, handoff_message=HANDOFF_MESSAGE
    )
    summaries = _forbid_summary(monkeypatch)
    check = ScriptedCheck(default=True)

    result = await process_turn(
        db_session, conversation_id, messages, _handoff_runtime(), supersede_check=check
    )

    assert isinstance(result, Message)
    assert result.content == HANDOFF_MESSAGE
    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )
    assert summaries == []


async def test_humanized_tool_handoff_sends_every_bubble_despite_a_newer_message(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, humanize=True, handoff_message=MULTI_PART_HANDOFF
    )
    # Nothing newer before delivery, then a newer message between bubbles.
    check = ScriptedCheck([False], default=True)

    result = await process_turn(
        db_session, conversation_id, messages, _handoff_runtime(), supersede_check=check
    )

    assert isinstance(result, Message)
    assert adapter.sent == HANDOFF_PARTS
    assert await _stored_replies(test_engine, conversation_id) == HANDOFF_PARTS
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )


# --- E4.6 -----------------------------------------------------------------------


async def test_superseded_turn_marks_its_own_run_not_another(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, humanize=False, handoff_message=HANDOFF_MESSAGE
    )
    other_run_id = await _other_turns_run(db_session, conversation_id)
    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content="Claro, posso ajudar.")]))

    result = await process_turn(
        db_session, conversation_id, messages, runtime, supersede_check=ScriptedCheck()
    )

    assert result == Superseded(0)
    assert adapter.sent == []
    statuses = await _run_statuses(test_engine, conversation_id)
    assert statuses.pop(other_run_id) == AgentRunStatus.SUCCESS
    assert list(statuses.values()) == [AgentRunStatus.SUPERSEDED]


async def test_superseded_turn_without_a_run_marks_nothing(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, humanize=False, handoff_message=HANDOFF_MESSAGE
    )
    other_run_id = await _other_turns_run(db_session, conversation_id)

    result = await process_turn(
        db_session, conversation_id, messages, PlainRuntime(), supersede_check=ScriptedCheck()
    )

    assert result == Superseded(0)
    assert adapter.sent == []
    assert await _run_statuses(test_engine, conversation_id) == {
        other_run_id: AgentRunStatus.SUCCESS
    }
