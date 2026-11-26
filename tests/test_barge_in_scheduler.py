"""Scheduler-level barge-in (ticket 23): Redis-backed supersede probe + the
requeue that follows a `Superseded` outcome.

`tests/test_barge_in_turn.py` (ticket 22) proves `process_turn`'s two
checkpoints in isolation with a *scripted* `supersede_check`. This file drives
the real pipeline end to end — ingest -> debounce -> `scheduler.tick` — with
the scheduler's actual Redis-backed probe (`debounce.has_pending`), and
proves the requeue: a superseded turn's batch goes back on the pending list
so the very next tick answers the old + new messages together, in one reply.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.conversations import debounce
from app.conversations import turn as turn_module
from app.conversations.debounce import pending_key
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido 12345 aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")
MERGED_REPLY = "Certo, atualizei para o pedido 99 - chega amanhã até as 18h."


class RecordingRuntime:
    """A `TurnRuntime` that records every `input_text` it was called with."""

    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[str] = []
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return self.reply


class InterruptOnFirstCallRuntime(RecordingRuntime):
    """Pushes a new inbound message onto the pending list mid-LLM-call, once.

    Mirrors `tests/test_debounce.py`'s `LateMessageRuntime`, but only fires on
    the first call so the merged-batch turn (the next tick) runs to
    completion instead of superseding itself forever.
    """

    def __init__(self, replies: list[str], on_first_call) -> None:
        super().__init__()
        self.replies = list(replies)
        self._on_first_call = on_first_call
        self._fired = False

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        if not self._fired:
            self._fired = True
            await self._on_first_call()
        return self.replies.pop(0)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, *, humanize: bool = True) -> tuple[dict, str]:
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


async def _send(client, headers, channel_id, text) -> tuple[uuid.UUID, uuid.UUID]:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["message_id"]), uuid.UUID(body["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _replies(db_session, conversation_id: uuid.UUID) -> list[str]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT,
            Message.type == "text",
        )
        .order_by(Message.seq)
    )
    return [row.content for row in result.scalars().all()]


async def _inbound(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _agent_runs(db_session, conversation_id: uuid.UUID) -> list[AgentRun]:
    result = await db_session.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.created_at, AgentRun.id)
    )
    return list(result.scalars().all())


class RecordingAdapter:
    """Stands in for the real adapter: records the outbound call sequence."""

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

    @property
    def sent(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "send"]


async def test_interrupt_after_first_bubble_merges_into_one_reply(
    client, fake_redis, db_session, monkeypatch
):
    """(a)+(b)+(c): interrupt between bubbles, one merged reply, nothing lost
    or answered twice.
    """
    headers, channel_id = await _setup(client, humanize=True)
    first_id, conversation_id = await _send(
        client, headers, channel_id, "Onde está meu pedido 12345?"
    )
    await _drain_jobs(fake_redis)

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    interrupt_ids: list[uuid.UUID] = []
    sleep_calls = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        # Land the interrupting message during the first bubble's pacing
        # pause: part 1 still goes out, checkpoint 2 then catches it before
        # part 2.
        if sleep_calls == 1:
            interrupt_id, _ = await _send(
                client, headers, channel_id, "na verdade é o pedido 99"
            )
            interrupt_ids.append(interrupt_id)
            await _drain_jobs(fake_redis)

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    provider = FakeLLMProvider(
        [LLMResponse(content=MULTI_PART_REPLY), LLMResponse(content=MERGED_REPLY)]
    )
    runtime = AgentRuntime(provider=provider)
    factory = _session_factory(db_session)

    # Tick 1: cut short after part 1.
    processed = await tick(fake_redis, factory, runtime, now=time.time() + 5)
    assert processed == []
    assert adapter.sent == [PARTS[0]]
    assert await _replies(db_session, conversation_id) == [PARTS[0]]
    runs = await _agent_runs(db_session, conversation_id)
    assert [r.status for r in runs] == [AgentRunStatus.SUPERSEDED]

    interrupt_id = interrupt_ids[0]
    pending = await fake_redis.lrange(pending_key(conversation_id), 0, -1)
    assert len(pending) == 2

    # Tick 2: the merged batch (old + new) answered as one reply.
    processed = await tick(fake_redis, factory, runtime, now=time.time() + 6)
    assert processed == [conversation_id]
    assert len(provider.calls) == 2
    merged_texts = [message.content or "" for message in provider.calls[1]["messages"]]
    assert any("12345" in text for text in merged_texts)
    assert any("99" in text for text in merged_texts)

    replies = await _replies(db_session, conversation_id)
    assert replies == [PARTS[0], MERGED_REPLY]
    # (c) never-two-replies: no reply text sent more than once.
    assert len(replies) == len(set(replies))

    runs = await _agent_runs(db_session, conversation_id)
    assert [r.status for r in runs] == [AgentRunStatus.SUPERSEDED, AgentRunStatus.SUCCESS]

    # Tick 3: nothing left to do.
    assert await fake_redis.llen(pending_key(conversation_id)) == 0
    assert await tick(fake_redis, factory, runtime, now=time.time() + 20) == []

    # (b) no-lost-message: every inbound message shows up in some call.
    inbound_ids = {message.id for message in await _inbound(db_session, conversation_id)}
    assert inbound_ids == {first_id, interrupt_id}
    seen_ids = {
        message.id
        for message in await _inbound(db_session, conversation_id)
        for call in provider.calls
        for sent in call["messages"]
        if message.content in (sent.content or "")
    }
    assert seen_ids == inbound_ids


async def test_interrupt_during_llm_call_merges_batch(client, fake_redis, db_session, monkeypatch):
    """(d) Interrupt during the LLM call itself, on a humanized channel:
    checkpoint 1 catches it before any bubble goes out.
    """
    headers, channel_id = await _setup(client, humanize=True)
    first_id, conversation_id = await _send(
        client, headers, channel_id, "Onde está meu pedido 12345?"
    )
    await _drain_jobs(fake_redis)

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    interrupt_ids: list[uuid.UUID] = []

    async def push_interrupt() -> None:
        interrupt_id, _ = await _send(client, headers, channel_id, "na verdade é o pedido 99")
        interrupt_ids.append(interrupt_id)
        await _drain_jobs(fake_redis)

    runtime = InterruptOnFirstCallRuntime(
        ["resposta que nunca deveria sair", MERGED_REPLY], push_interrupt
    )
    factory = _session_factory(db_session)

    processed = await tick(fake_redis, factory, runtime, now=time.time() + 5)
    assert processed == []
    assert adapter.sent == []
    assert await _replies(db_session, conversation_id) == []

    processed = await tick(fake_redis, factory, runtime, now=time.time() + 6)
    assert processed == [conversation_id]
    assert runtime.calls[1] == "Onde está meu pedido 12345?\nna verdade é o pedido 99"
    assert await _replies(db_session, conversation_id) == [MERGED_REPLY]

    assert await tick(fake_redis, factory, runtime, now=time.time() + 20) == []


async def test_human_handoff_conversation_keeps_todays_behavior(client, fake_redis, db_session):
    """(e) A handed-off conversation is untouched: no supersede, no requeue."""
    headers, channel_id = await _setup(client, humanize=True)
    _first_id, conversation_id = await _send(
        client, headers, channel_id, "Quero falar com uma pessoa"
    )
    await _drain_jobs(fake_redis)

    conversation = await db_session.get(Conversation, conversation_id)
    conversation.state = ConversationState.HUMAN_HANDOFF
    await db_session.commit()

    # A second message piles onto the same pending batch before the tick runs.
    await _send(client, headers, channel_id, "alô?")
    await _drain_jobs(fake_redis)

    runtime = RecordingRuntime()
    processed = await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    assert processed == [conversation_id]  # drained...
    assert runtime.calls == []  # ...but the AI never ran
    assert await _replies(db_session, conversation_id) == []
    assert await fake_redis.llen(pending_key(conversation_id)) == 0  # not requeued


async def test_probe_failure_delivers_reply_in_full(client, fake_redis, db_session, monkeypatch):
    """(f) A broken supersede probe degrades to today's behavior: the reply
    still goes out in full.
    """
    headers, channel_id = await _setup(client, humanize=True)
    await _send(client, headers, channel_id, "Onde está meu pedido 12345?")
    await _drain_jobs(fake_redis)

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    original_has_pending = debounce.has_pending
    calls = {"count": 0}

    async def flaky_has_pending(redis, conversation_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("redis is down")
        return await original_has_pending(redis, conversation_id)

    monkeypatch.setattr(debounce, "has_pending", flaky_has_pending)

    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content=MULTI_PART_REPLY)]))
    processed = await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    assert len(processed) == 1
    assert adapter.sent == PARTS
    assert await _replies(db_session, processed[0]) == PARTS
