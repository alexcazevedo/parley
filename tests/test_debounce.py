"""Debounce + per-conversation lock + turn execution.

Every test drives `scheduler.tick` explicitly (with an explicit `now`) so the
timing is deterministic — no sleeping on the real 250ms cadence.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.conversations import lock
from app.conversations.debounce import DUE_KEY, pending_key
from app.conversations.turn import EchoRuntime
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2


class RecordingRuntime:
    """A `TurnRuntime` that records every `input_text` it was called with."""

    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[str] = []
        self.reply = reply

    async def run(self, conversation: Conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return self.reply


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


async def _setup_channel(client) -> tuple[dict, str]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    tenant = tenant_response.json()
    headers = _auth(tenant["api_key"])
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"]


async def _send(client, headers, channel_id, text, user_id="user-1") -> dict:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": user_id, "message": text},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()


async def _drain_jobs(fake_redis) -> None:
    """Run the worker job loop until the queue is empty."""
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _assistant_messages(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def test_three_rapid_messages_become_one_turn(client, fake_redis, db_session):
    headers, channel_id = await _setup_channel(client)

    texts = ["Oi", "Queria saber onde está", "meu pedido 12345"]
    results = [await _send(client, headers, channel_id, text) for text in texts]
    conversation_id = uuid.UUID(results[0]["conversation_id"])
    assert {r["conversation_id"] for r in results} == {str(conversation_id)}

    await _drain_jobs(fake_redis)

    # All three queued on one pending list, one due entry.
    assert await fake_redis.llen(pending_key(conversation_id)) == 3
    assert await fake_redis.zcard(DUE_KEY) == 1

    runtime = RecordingRuntime(reply="pedido a caminho")
    factory = _session_factory(db_session)

    # Window still open -> nothing runs.
    assert await tick(fake_redis, factory, runtime, now=time.time()) == []
    assert runtime.calls == []

    processed = await tick(
        fake_redis, factory, runtime, now=time.time() + DEBOUNCE_SECONDS + 1
    )
    assert processed == [conversation_id]
    assert runtime.calls == ["Oi\nQueria saber onde está\nmeu pedido 12345"]

    replies = await _assistant_messages(db_session, conversation_id)
    assert [m.content for m in replies] == ["pedido a caminho"]

    # Pending drained, conversation no longer due, lock released.
    assert await fake_redis.llen(pending_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0

    # A second tick finds nothing and must not re-run the turn.
    assert await tick(fake_redis, factory, runtime, now=time.time() + 10) == []
    assert len(runtime.calls) == 1


async def test_second_scheduler_requeues_while_lock_is_held(client, fake_redis, db_session):
    headers, channel_id = await _setup_channel(client)
    result = await _send(client, headers, channel_id, "Oi")
    conversation_id = uuid.UUID(result["conversation_id"])
    await _drain_jobs(fake_redis)

    runtime = RecordingRuntime()
    factory = _session_factory(db_session)

    # Scheduler A is mid-turn: it holds the lock.
    token = await lock.acquire(fake_redis, conversation_id)
    assert token is not None

    # Scheduler B claims the due entry, can't lock, and puts it back.
    assert await tick(fake_redis, factory, runtime, now=time.time() + 1) == []
    assert runtime.calls == []
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None
    assert await fake_redis.llen(pending_key(conversation_id)) == 1

    # Scheduler A finishes; a foreign token must not be able to release.
    assert await lock.release(fake_redis, conversation_id, "not-my-token") is False
    assert await lock.release(fake_redis, conversation_id, token) is True

    processed = await tick(fake_redis, factory, runtime, now=time.time() + 5)
    assert processed == [conversation_id]
    assert runtime.calls == ["Oi"]
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0


async def test_human_handoff_conversation_gets_no_assistant_message(
    client, fake_redis, db_session
):
    headers, channel_id = await _setup_channel(client)
    result = await _send(client, headers, channel_id, "Quero falar com uma pessoa")
    conversation_id = uuid.UUID(result["conversation_id"])

    conversation = await db_session.get(Conversation, conversation_id)
    conversation.state = ConversationState.HUMAN_HANDOFF
    await db_session.commit()

    await _drain_jobs(fake_redis)

    runtime = RecordingRuntime()
    processed = await tick(
        fake_redis, _session_factory(db_session), runtime, now=time.time() + 5
    )

    assert processed == [conversation_id]  # drained...
    assert runtime.calls == []  # ...but the agent never ran
    assert await _assistant_messages(db_session, conversation_id) == []
    assert await fake_redis.llen(pending_key(conversation_id)) == 0


async def test_message_arriving_during_turn_requeues_conversation(
    client, fake_redis, db_session
):
    headers, channel_id = await _setup_channel(client)
    first = await _send(client, headers, channel_id, "Oi")
    conversation_id = uuid.UUID(first["conversation_id"])
    await _drain_jobs(fake_redis)

    class LateMessageRuntime(RecordingRuntime):
        async def run(self, conversation, input_text):
            # Simulates a message landing on the pending list mid-turn.
            await fake_redis.rpush(pending_key(conversation_id), str(uuid.uuid4()))
            return await super().run(conversation, input_text)

    runtime = LateMessageRuntime()
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    # The mid-turn push supersedes the turn (the scheduler's supersede probe
    # sees it): the runtime still ran once, but no reply went out, the
    # conversation is re-armed, and the drained "Oi" batch is requeued ahead
    # of the interrupting message.
    assert runtime.calls == ["Oi"]
    assert await _assistant_messages(db_session, conversation_id) == []
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None
    pending = await fake_redis.lrange(pending_key(conversation_id), 0, -1)
    assert len(pending) == 2
    assert uuid.UUID(pending[0]) == uuid.UUID(first["message_id"])


async def test_echo_runtime_is_the_default(client, fake_redis, db_session):
    headers, channel_id = await _setup_channel(client)
    result = await _send(client, headers, channel_id, "Oi")
    conversation_id = uuid.UUID(result["conversation_id"])
    await _drain_jobs(fake_redis)

    await tick(fake_redis, _session_factory(db_session), EchoRuntime(), now=time.time() + 5)

    replies = await _assistant_messages(db_session, conversation_id)
    assert [m.content for m in replies] == ["echo: Oi"]
