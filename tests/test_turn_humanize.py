"""The humanizer wired into `process_turn` (ticket 16).

Same pipeline as `tests/test_agent_loop.py` (ingest -> debounce -> scheduler
tick -> AgentRuntime), with the channel adapter swapped for a recorder and
`asyncio.sleep` neutralized so the paced delays are asserted, not slept.
"""

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.agents.runtime import HUMANIZE_STYLE_HINT, AgentRuntime
from app.config import get_settings
from app.conversations import turn as turn_module
from app.conversations.humanize import typing_delay_seconds
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)


class RecordingAdapter:
    """Stands in for the real adapter: records the outbound call sequence.

    `send_message` also records how many assistant text rows the turn's session
    can already see, which is what proves the part was persisted *before* it
    was sent.
    """

    def __init__(self, session, conversation_id: uuid.UUID, events: list) -> None:
        self.session = session
        self.conversation_id = conversation_id
        self.events = events
        self.mark_read_error: Exception | None = None

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        self.events.append(("mark_read", external_message_id))
        if self.mark_read_error is not None:
            raise self.mark_read_error

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        self.events.append(("typing", duration_seconds))

    async def send_message(self, message, contact, channel) -> None:
        result = await self.session.execute(
            select(Message).where(
                Message.conversation_id == self.conversation_id,
                Message.direction == MessageDirection.OUT,
                Message.role == MessageRole.ASSISTANT,
                Message.type == "text",
            )
        )
        self.events.append(("send", message.content, len(result.scalars().all())))


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, *, humanize: bool) -> tuple[dict, str]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent.json()["id"],
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


async def _run(client, fake_redis, db_session, monkeypatch, *, humanize: bool, reply: str):
    """One full turn; returns (conversation_id, adapter, events, delays)."""
    headers, channel_id = await _setup(client, humanize=humanize)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido")
    await _drain_jobs(fake_redis)

    events: list = []
    adapter = RecordingAdapter(db_session, conversation_id, events)
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        events.append(("sleep", seconds))

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    provider = FakeLLMProvider([LLMResponse(content=reply)])
    runtime = AgentRuntime(provider=provider)
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    return conversation_id, adapter, events, delays, provider


async def test_humanized_turn_marks_read_then_paces_each_part(
    client, fake_redis, db_session, monkeypatch
):
    conversation_id, _adapter, events, delays, provider = await _run(
        client, fake_redis, db_session, monkeypatch, humanize=True, reply=MULTI_PART_REPLY
    )

    parts = MULTI_PART_REPLY.split("\n\n")
    assert len(parts) == 3

    # mark_read once, up front; then typing -> pause -> send, per part, in order.
    assert [event[0] for event in events] == ["mark_read", *(["typing", "sleep", "send"] * 3)]
    sends = [event for event in events if event[0] == "send"]
    assert [event[1] for event in sends] == parts
    # Each part was persisted before it was sent (1 row visible, then 2, then 3).
    assert [event[2] for event in sends] == [1, 2, 3]

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == parts

    settings = get_settings()
    typing_delays = [event[1] for event in events if event[0] == "typing"]
    assert typing_delays == delays
    assert all(
        settings.humanize_min_delay_seconds <= d <= settings.humanize_max_delay_seconds
        for d in delays
    )
    assert delays == [typing_delay_seconds(part, settings) for part in parts]

    # The style hint reached the model.
    system_prompt = provider.calls[0]["messages"][0].content
    assert system_prompt.endswith(HUMANIZE_STYLE_HINT)


async def test_humanize_off_keeps_the_single_message_flow(
    client, fake_redis, db_session, monkeypatch
):
    conversation_id, _adapter, events, delays, provider = await _run(
        client, fake_redis, db_session, monkeypatch, humanize=False, reply=MULTI_PART_REPLY
    )

    assert [event[0] for event in events] == ["send"]
    assert events[0][1] == MULTI_PART_REPLY
    assert delays == []

    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == [MULTI_PART_REPLY]

    assert HUMANIZE_STYLE_HINT not in provider.calls[0]["messages"][0].content


async def test_mark_read_failure_does_not_cost_the_reply(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id = await _setup(client, humanize=True)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    events: list = []
    adapter = RecordingAdapter(db_session, conversation_id, events)
    adapter.mark_read_error = RuntimeError("evolution returned 500")
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content="Tudo certo!")]))
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    assert [event[0] for event in events] == ["mark_read", "typing", "send"]
    rows = await _replies(db_session, conversation_id)
    assert [row.content for row in rows] == ["Tudo certo!"]


def test_turn_module_sleeps_through_asyncio() -> None:
    """Guards the monkeypatch above: the pacing pause must stay `asyncio.sleep`."""
    assert turn_module.asyncio is asyncio
