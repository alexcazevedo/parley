"""PRODUCT.md "Success demo" for the reliability run, end to end.

Each test goes through the real pipeline (API -> queue -> `run_once` ->
`tick`) with fake Redis and the test database, and asserts on what was stored
and sent. Only the model (a stub runtime) and the channel provider (a
recording adapter) are replaced. Every `tick` gets an explicit `now`, so the
retry backoff is driven, never slept; the slow turns really sleep for a
fraction of a second.
"""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.conversations import lock
from app.conversations import turn as turn_module
from app.conversations.debounce import DUE_KEY, inflight_key, pending_key
from app.conversations.retry import MAX_TURN_ATTEMPTS, RETRY_BACKOFF_SECONDS, TURN_DLQ_KEY
from app.conversations.turn import TURN_FAILED_MESSAGE
from app.models.message import Message, MessageRole
from app.queue.redis_queue import (
    JOBS_KEY,
    PROCESSING_KEY_PREFIX,
    WORKER_ALIVE_PREFIX,
    RedisQueue,
)
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")


class SendFailed(Exception):
    """What a channel provider being down looks like to the turn."""


class Runtime:
    """Answers `reply` (or `re: <text>`), after `delay` seconds; records spans."""

    def __init__(self, reply: str | None = None, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[str] = []
        self.spans: dict[str, tuple[float, float]] = {}
        self.started = asyncio.Event()

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        began = time.monotonic()
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.spans[input_text] = (began, time.monotonic())
        return self.reply if self.reply is not None else f"re: {input_text}"


class Adapter:
    """Records every send attempt; `fail_sends` (1-based) or `fail_always` raise."""

    def __init__(self, *, fail_sends: set[int] | None = None, fail_always: bool = False) -> None:
        self.fail_sends = fail_sends or set()
        self.fail_always = fail_always
        self.attempts: list[tuple[uuid.UUID, str]] = []
        self.delivered: list[tuple[uuid.UUID, str]] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        self.attempts.append((message.conversation_id, message.content))
        if self.fail_always or len(self.attempts) in self.fail_sends:
            raise SendFailed("provider returned 503")
        self.delivered.append((message.conversation_id, message.content))

    def delivered_to(self, conversation_id: uuid.UUID) -> list[str]:
        return [text for cid, text in self.delivered if cid == conversation_id]

    def attempted_for(self, conversation_id: uuid.UUID) -> list[str]:
        return [text for cid, text in self.attempts if cid == conversation_id]


@pytest.fixture
def adapter(monkeypatch):
    def install(instance: Adapter) -> Adapter:
        monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: instance)
        return instance

    return install


@pytest.fixture
def sessions(test_engine):
    """A fresh session per call: concurrent turns cannot share one."""
    maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


async def _post_message(
    client, text: str, *, humanize: bool = False
) -> tuple[uuid.UUID, uuid.UUID]:
    """A new tenant, agent, channel and one customer message through the API."""
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent.json()["id"],
            "config": {"humanize": True} if humanize else {},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["message_id"]), uuid.UUID(body["conversation_id"])


async def _ingest(fake_redis) -> None:
    """A healthy worker takes every queued job into the debounce batch."""
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _replies(sessions, conversation_id: uuid.UUID) -> list[str]:
    async with sessions() as session:
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.ASSISTANT,
            )
            .order_by(Message.seq)
        )
        return [row.content for row in result.scalars().all()]


async def _dead_letters(fake_redis) -> list[dict]:
    return [json.loads(raw) for raw in await fake_redis.lrange(TURN_DLQ_KEY, 0, -1)]


# --- 1. a job taken by a worker that dies is answered once -----------------------


async def test_job_of_a_dead_worker_is_reaped_and_answered_once(
    client, fake_redis, sessions, adapter
):
    sent = adapter(Adapter())
    runtime = Runtime()
    message_id, conversation_id = await _post_message(client, "Oi")

    # The worker takes the job, then dies: no debounce entry, no ack.
    dying = RedisQueue(fake_redis, worker_id="dying")
    await dying.heartbeat()
    job = await dying.dequeue(timeout=1)
    assert job is not None and job.message_id == message_id
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert await fake_redis.llen(f"{PROCESSING_KEY_PREFIX}dying") == 1
    await fake_redis.delete(f"{WORKER_ALIVE_PREFIX}dying")  # its alive key expires

    # Nothing is due yet, so nothing is answered: the message is not lost, just waiting.
    assert await tick(fake_redis, sessions, runtime, now=time.time() + 5) == []
    assert runtime.calls == []

    survivor = RedisQueue(fake_redis, worker_id="survivor")
    await survivor.heartbeat()
    assert await survivor.reap_dead_workers() == 1
    assert await fake_redis.llen(f"{PROCESSING_KEY_PREFIX}dying") == 0
    assert await fake_redis.llen(JOBS_KEY) == 1

    assert await run_once(survivor, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) == job
    assert await fake_redis.llen(f"{PROCESSING_KEY_PREFIX}survivor") == 0

    now = time.time() + 5
    assert await tick(fake_redis, sessions, runtime, now=now) == [conversation_id]
    assert await tick(fake_redis, sessions, runtime, now=now + 1000) == []

    assert runtime.calls == ["Oi"]
    assert sent.delivered_to(conversation_id) == ["re: Oi"]
    assert await _replies(sessions, conversation_id) == ["re: Oi"]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await survivor.reap_dead_workers() == 0


# --- 2. a send that always fails: retried, dead-lettered, one error message ----------


async def test_send_that_always_fails_is_retried_then_dead_lettered_with_one_error_message(
    client, fake_redis, sessions, adapter
):
    sent = adapter(Adapter(fail_always=True))
    runtime = Runtime("ok")
    message_id, conversation_id = await _post_message(client, "Oi")
    await _ingest(fake_redis)
    now = time.time() + 5
    first_retry = now + RETRY_BACKOFF_SECONDS[0]
    second_retry = first_retry + RETRY_BACKOFF_SECONDS[1]

    assert await tick(fake_redis, sessions, runtime, now=now) == []
    assert await tick(fake_redis, sessions, runtime, now=first_retry) == []
    assert await _dead_letters(fake_redis) == []
    assert await tick(fake_redis, sessions, runtime, now=second_retry) == []

    assert len(runtime.calls) == MAX_TURN_ATTEMPTS == 3
    entries = await _dead_letters(fake_redis)
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == str(conversation_id)
    assert entries[0]["message_ids"] == [str(message_id)]
    assert entries[0]["partly_delivered"] is False
    assert entries[0]["attempts"] == MAX_TURN_ATTEMPTS
    assert "SendFailed" in entries[0]["error"]

    # Exactly one stored error message, and no reply was ever stored or delivered.
    assert await _replies(sessions, conversation_id) == [TURN_FAILED_MESSAGE]
    assert sent.delivered == []
    assert sent.attempted_for(conversation_id) == ["ok", "ok", "ok", TURN_FAILED_MESSAGE]

    # Nothing is left to run, however late a worker looks.
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await tick(fake_redis, sessions, runtime, now=second_retry + 1000) == []
    assert len(runtime.calls) == MAX_TURN_ATTEMPTS
    assert await _replies(sessions, conversation_id) == [TURN_FAILED_MESSAGE]
    assert len(await _dead_letters(fake_redis)) == 1


# --- 3. a partly delivered reply is not sent again ------------------------------------


async def test_reply_with_a_failed_second_bubble_is_not_resent_and_gets_no_error_message(
    client, fake_redis, sessions, adapter, monkeypatch
):
    sent = adapter(Adapter(fail_sends={2}))

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", no_sleep)  # humanized pauses
    runtime = Runtime(MULTI_PART_REPLY)
    message_id, conversation_id = await _post_message(client, "Oi", humanize=True)
    await _ingest(fake_redis)
    now = time.time() + 5

    assert await tick(fake_redis, sessions, runtime, now=now) == []
    assert await tick(fake_redis, sessions, runtime, now=now + 1000) == []

    assert len(runtime.calls) == 1  # never retried
    assert sent.delivered_to(conversation_id) == [PARTS[0]]
    assert sent.attempted_for(conversation_id) == PARTS[:2]  # bubble 1 once, bubble 2 once
    assert await _replies(sessions, conversation_id) == [PARTS[0]]
    assert TURN_FAILED_MESSAGE not in await _replies(sessions, conversation_id)

    entries = await _dead_letters(fake_redis)
    assert len(entries) == 1
    assert entries[0]["partly_delivered"] is True
    assert entries[0]["message_ids"] == [str(message_id)]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.exists(pending_key(conversation_id)) == 0


# --- 4. two slow conversations progress at the same time on one worker ----------------


async def test_two_slow_conversations_overlap_in_time_on_one_worker(
    client, fake_redis, sessions, adapter
):
    sent = adapter(Adapter())
    runtime = Runtime(delay=0.3)
    _, first = await _post_message(client, "primeira")
    _, second = await _post_message(client, "segunda")
    await _ingest(fake_redis)

    started = time.monotonic()
    processed = await tick(fake_redis, sessions, runtime, now=time.time() + 5)
    elapsed = time.monotonic() - started

    assert processed == [first, second]
    (a_start, a_end), (b_start, b_end) = runtime.spans["primeira"], runtime.spans["segunda"]
    assert a_start < b_end and b_start < a_end  # each began before the other ended
    assert elapsed < 0.55  # two 0.3 s turns, side by side (one after the other: 0.6 s)
    assert await _replies(sessions, first) == ["re: primeira"]
    assert await _replies(sessions, second) == ["re: segunda"]
    assert sent.delivered_to(first) == ["re: primeira"]
    assert sent.delivered_to(second) == ["re: segunda"]


# --- 5. a turn longer than the lock's lifetime never runs twice in parallel -----------


async def test_turn_longer_than_the_lock_ttl_is_never_run_twice_in_parallel(
    client, fake_redis, sessions, adapter, monkeypatch
):
    ttl_ms = 300
    monkeypatch.setattr(lock, "LOCK_TTL_MS", ttl_ms)
    monkeypatch.setattr(lock, "LOCK_RENEW_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(lock.acquire, "__defaults__", (ttl_ms,))
    monkeypatch.setattr(lock.renew, "__defaults__", (ttl_ms,))
    sent = adapter(Adapter())
    runtime = Runtime(delay=0.8)  # more than two lock lifetimes
    _, conversation_id = await _post_message(client, "Oi")
    await _ingest(fake_redis)
    now = time.time() + 5
    key = lock.lock_key(conversation_id)

    first = asyncio.create_task(tick(fake_redis, sessions, runtime, now=now))
    await asyncio.wait_for(runtime.started.wait(), 3)
    token = await fake_redis.get(key)
    assert token is not None

    await asyncio.sleep(0.5)  # well past the original TTL
    assert await fake_redis.get(key) == token  # still ours: renewed, not expired

    # Another worker, whose clock says the claim timed out long ago, finds the
    # conversation due but the lock held, so it does not start a second turn.
    assert await tick(fake_redis, sessions, runtime, now=now + 1000) == []
    assert runtime.calls == ["Oi"]

    assert await first == [conversation_id]
    assert runtime.calls == ["Oi"]
    assert sent.delivered_to(conversation_id) == ["re: Oi"]
    assert await _replies(sessions, conversation_id) == ["re: Oi"]
    assert await fake_redis.exists(key) == 0
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
