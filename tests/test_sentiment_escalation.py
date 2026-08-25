"""Sentiment-triggered escalation wired into `process_turn` (ticket 21).

Same pipeline as tests/test_turn_humanize.py (ingest -> debounce -> scheduler
tick), with `turn.score_frustration` monkeypatched so no classifier LLM call
happens and the score is the only variable under test. The runtime is a
recorder: an escalating turn must never reach it.

The handoff summarizer's provider is scripted too, so the warm-handoff brief
is deterministic instead of a live call.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

import app.handoff.summarizer as summarizer_module
from app.conversations import turn as turn_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."
NORMAL_REPLY = "Claro! Deixa eu verificar isso pra você."
BRIEF_TEXT = "Usuário irritado com atraso do pedido; bot não resolveu."


class RecordingRuntime:
    """Text-in/text-out runtime that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return NORMAL_REPLY


class RecordingAdapter:
    """Stands in for the real adapter: records outbound calls, sends nothing."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        self.events.append(("mark_read", external_message_id))

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        self.events.append(("typing", duration_seconds))

    async def send_message(self, message, contact, channel) -> None:
        self.events.append(("send", message.content))


class ScoreRecorder:
    """Stand-in for `score_frustration` — records calls, returns a fixed score."""

    def __init__(self, score: float | None) -> None:
        self.score = score
        self.calls: list[str] = []

    async def __call__(self, session, conversation, agent, input_text: str) -> float | None:
        self.calls.append(input_text)
        return self.score


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(
    client, db_session, *, humanize: bool, sentiment_enabled: bool
) -> tuple[dict, str]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent_response = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Você é o atendente da Acme.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent_response.status_code == 201, agent_response.text
    # Neither field is exposed by the agents API (out of this ticket's scope),
    # so they are set straight on the row.
    agent = await db_session.get(Agent, uuid.UUID(agent_response.json()["id"]))
    agent.sentiment_escalation_enabled = sentiment_enabled
    agent.handoff_message = HANDOFF_MESSAGE
    await db_session.commit()

    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent_response.json()["id"],
            "config": {"humanize": humanize} if humanize else {},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"]


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


async def _replies(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT,
            Message.type == "text",
        )
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def _handoffs(db_session, conversation_id: uuid.UUID) -> list[Handoff]:
    result = await db_session.execute(
        select(Handoff).where(Handoff.conversation_id == conversation_id)
    )
    return list(result.scalars().all())


async def _run(
    client,
    fake_redis,
    db_session,
    monkeypatch,
    *,
    score: float | None,
    humanize: bool = False,
    sentiment_enabled: bool = True,
    text: str = "isso nao resolve nada, que atendimento horrivel",
):
    """One full turn; returns (conversation_id, runtime, adapter, scorer)."""
    headers, channel_id = await _setup(
        client, db_session, humanize=humanize, sentiment_enabled=sentiment_enabled
    )
    conversation_id = await _send(client, headers, channel_id, text)
    await _drain_jobs(fake_redis)

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    scorer = ScoreRecorder(score)
    monkeypatch.setattr(turn_module, "score_frustration", scorer)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)]),
    )

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    runtime = RecordingRuntime()
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    return conversation_id, runtime, adapter, scorer


async def test_frustrated_batch_escalates_instead_of_answering(
    client, fake_redis, db_session, monkeypatch
):
    conversation_id, runtime, adapter, scorer = await _run(
        client, fake_redis, db_session, monkeypatch, score=0.9
    )

    # The agent never ran: no AI answer once we decided to escalate.
    assert runtime.calls == []
    assert scorer.calls == ["isso nao resolve nada, que atendimento horrivel"]

    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.refresh(conversation)
    assert conversation.state == ConversationState.HUMAN_HANDOFF

    handoffs = await _handoffs(db_session, conversation_id)
    assert len(handoffs) == 1
    assert handoffs[0].requested_by == HandoffRequestedBy.SYSTEM
    assert "0.90" in handoffs[0].reason
    assert handoffs[0].summary == BRIEF_TEXT

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == [HANDOFF_MESSAGE]
    assert [event for event in adapter.events if event[0] == "send"] == [
        ("send", HANDOFF_MESSAGE)
    ]


async def test_calm_batch_takes_the_normal_turn(client, fake_redis, db_session, monkeypatch):
    conversation_id, runtime, _adapter, scorer = await _run(
        client, fake_redis, db_session, monkeypatch, score=0.5, text="oi, tudo bem?"
    )

    assert runtime.calls == ["oi, tudo bem?"]
    assert scorer.calls == ["oi, tudo bem?"]
    assert await _handoffs(db_session, conversation_id) == []

    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.refresh(conversation)
    assert conversation.state == ConversationState.ACTIVE_AI

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == [NORMAL_REPLY]


async def test_classifier_failure_proceeds_with_the_normal_turn(
    client, fake_redis, db_session, monkeypatch
):
    conversation_id, runtime, _adapter, scorer = await _run(
        client, fake_redis, db_session, monkeypatch, score=None
    )

    assert len(runtime.calls) == 1
    assert len(scorer.calls) == 1
    assert await _handoffs(db_session, conversation_id) == []

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == [NORMAL_REPLY]


async def test_flag_off_never_calls_the_classifier(client, fake_redis, db_session, monkeypatch):
    conversation_id, runtime, _adapter, scorer = await _run(
        client, fake_redis, db_session, monkeypatch, score=0.9, sentiment_enabled=False
    )

    assert scorer.calls == []
    assert len(runtime.calls) == 1
    assert await _handoffs(db_session, conversation_id) == []

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == [NORMAL_REPLY]


async def test_humanized_channel_still_delivers_the_handoff_message(
    client, fake_redis, db_session, monkeypatch
):
    conversation_id, runtime, adapter, _scorer = await _run(
        client, fake_redis, db_session, monkeypatch, score=0.9, humanize=True
    )

    assert runtime.calls == []
    sends = [event for event in adapter.events if event[0] == "send"]
    assert len(sends) >= 1
    assert "typing" in [event[0] for event in adapter.events]

    rows = await _replies(db_session, conversation_id)
    assert len(rows) >= 1
    assert "".join(row.content for row in rows) == HANDOFF_MESSAGE
    assert "".join(event[1] for event in sends) == HANDOFF_MESSAGE

    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.refresh(conversation)
    assert conversation.state == ConversationState.HUMAN_HANDOFF
