"""A handoff whose message failed to send still gets it on the retry (ticket 11).

The handoff itself is committed before the send of `agent.handoff_message`
(the tool ledger, or `_deliver`'s `end_transaction` on the sentiment path), so
a failed send leaves the conversation in `HUMAN_HANDOFF` with nothing said.
The retried turn of the same batch must send the message once and store it; a
further run of the same batch must send nothing.

Silent, as before: a handoff an operator asked for, and a handoff whose
message an earlier turn already delivered (a new batch gets nothing).

The reply rows carry the worker's clock and the handoff rows the database's,
so the "already answered" check is also run with the worker's clock an hour
ahead of and an hour behind the database's.
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import LockLost, TurnFailed, process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."
BRIEF_TEXT = "Cliente pediu atendimento humano."


class SwitchableAdapter:
    """Channel double: every send raises while `fail` is set, else is recorded."""

    def __init__(self) -> None:
        self.fail = False
        self.sent: list[str] = []

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        if self.fail:
            raise RuntimeError("provider returned 503")
        self.sent.append(message.content)


class MustNotBeCalledProvider:
    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise AssertionError("the model must not be called once handed off")


class PlainRuntime:
    """Text-in/text-out runtime for an ordinary AI answer."""

    async def run(self, conversation, input_text: str) -> str:
        return "Claro, posso ajudar."


@pytest.fixture
def adapter(monkeypatch) -> SwitchableAdapter:
    double = SwitchableAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: double)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)]),
    )
    return double


def _skew_worker_clock(monkeypatch, skew: timedelta) -> None:
    """`datetime.now` in app/conversations/turn.py runs `skew` off the database's."""
    real = datetime

    class SkewedDatetime(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + skew

    monkeypatch.setattr(turn_module, "datetime", SkewedDatetime)


async def _setup(client, fake_redis, db_session, *, sentiment: bool = False):
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
    agent.sentiment_escalation_enabled = sentiment
    await db_session.commit()
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": created_agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    channel_id = channel.json()["id"]
    conversation_id = await _send(client, fake_redis, headers, channel_id, "quero uma pessoa")
    return headers, channel_id, conversation_id


async def _send(client, fake_redis, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    return uuid.UUID(response.json()["conversation_id"])


async def _inbound(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
            Message.role == MessageRole.USER,
        )
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


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


def _silent_runtime() -> AgentRuntime:
    return AgentRuntime(
        provider=MustNotBeCalledProvider(), registry=ToolRegistry([BuiltinExecutor()])
    )


async def _failed_first_attempt(db_session, conversation_id, messages, adapter, runtime) -> None:
    adapter.fail = True
    with pytest.raises(TurnFailed) as failure:
        await process_turn(db_session, conversation_id, messages, runtime)
    assert failure.value.delivered_parts == 0
    adapter.fail = False


async def _retry_sends_once_then_nothing(
    db_session, test_engine, conversation_id, messages, adapter
) -> None:
    """The retry delivers the handoff message once; a third run sends nothing."""
    retried = await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert isinstance(retried, Message)
    assert retried.content == HANDOFF_MESSAGE
    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]

    third = await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert third is None
    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]
    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )


# --- the message is still owed ------------------------------------------------------


async def test_tool_handoff_whose_send_failed_is_delivered_by_the_retry(
    client, fake_redis, db_session, test_engine, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation_id)

    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _handoff_runtime())

    assert await _committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )
    assert adapter.sent == []
    await _retry_sends_once_then_nothing(
        db_session, test_engine, conversation_id, messages, adapter
    )


async def test_sentiment_handoff_whose_send_failed_is_delivered_by_the_retry(
    client, fake_redis, db_session, test_engine, adapter, monkeypatch
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session, sentiment=True)

    async def frustrated(session, conversation, agent, input_text) -> float:
        return 0.95

    monkeypatch.setattr(turn_module, "score_frustration", frustrated)
    messages = await _inbound(db_session, conversation_id)

    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _silent_runtime())

    query = select(Handoff).where(Handoff.conversation_id == conversation_id)
    [handoff] = (await db_session.execute(query)).scalars().all()
    assert handoff.requested_by == HandoffRequestedBy.SYSTEM
    await _retry_sends_once_then_nothing(
        db_session, test_engine, conversation_id, messages, adapter
    )


@pytest.mark.parametrize("skew", [timedelta(hours=1), timedelta(hours=-1)], ids=["ahead", "behind"])
async def test_owed_check_holds_when_the_worker_clock_differs_from_the_database(
    client, fake_redis, db_session, test_engine, adapter, monkeypatch, skew
):
    headers, channel_id, conversation_id = await _setup(client, fake_redis, db_session)
    _skew_worker_clock(monkeypatch, skew)
    # An ordinary AI answer first: its row carries the skewed worker clock.
    first = await process_turn(
        db_session, conversation_id, await _inbound(db_session, conversation_id), PlainRuntime()
    )
    assert isinstance(first, Message)
    adapter.sent.clear()
    await _send(client, fake_redis, headers, channel_id, "agora quero uma pessoa")
    messages = (await _inbound(db_session, conversation_id))[-1:]

    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _handoff_runtime())

    retried = await process_turn(db_session, conversation_id, messages, _silent_runtime())
    assert isinstance(retried, Message)
    assert adapter.sent == [HANDOFF_MESSAGE]
    third = await process_turn(db_session, conversation_id, messages, _silent_runtime())
    assert third is None
    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [
        "Claro, posso ajudar.",
        HANDOFF_MESSAGE,
    ]


async def test_owed_message_is_not_sent_once_the_lock_is_lost(
    client, fake_redis, db_session, test_engine, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation_id)
    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _handoff_runtime())

    async def not_ours() -> bool:
        return False

    result = await process_turn(
        db_session, conversation_id, messages, _silent_runtime(), ownership_check=not_ours
    )

    assert isinstance(result, LockLost)
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []


# --- the message is not owed --------------------------------------------------------


async def test_operator_handoff_gets_no_message(
    client, fake_redis, db_session, test_engine, adapter
):
    headers, _, conversation_id = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation_id)
    response = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "operador assumiu"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    result = await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert result is None
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []


async def test_new_batch_after_a_delivered_handoff_gets_no_message(
    client, fake_redis, db_session, test_engine, adapter
):
    headers, channel_id, conversation_id = await _setup(client, fake_redis, db_session)
    first = await process_turn(
        db_session, conversation_id, await _inbound(db_session, conversation_id), _handoff_runtime()
    )
    assert isinstance(first, Message)
    assert adapter.sent == [HANDOFF_MESSAGE]

    await _send(client, fake_redis, headers, channel_id, "alguém aí?")
    newer = (await _inbound(db_session, conversation_id))[-1:]
    result = await process_turn(db_session, conversation_id, newer, _silent_runtime())

    assert result is None
    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]
