"""A failed turn is retried, then dead-lettered (PRODUCT.md E3.2).

Any error during a turn puts the batch back: up to `MAX_TURN_ATTEMPTS`
attempts, `RETRY_BACKOFF_SECONDS` apart. After the last one the batch goes to
the dead-letter list and the customer gets `TURN_FAILED_MESSAGE` once. Once a
part of a reply reached the customer there is no retry: what was sent stays,
the batch is dead-lettered as partly delivered and no error message is added.

Every `tick` gets an explicit `now`, so the backoff is driven, never slept.
"""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import structlog
from sqlalchemy import select

from app.conversations import debounce, lock, retry
from app.conversations import turn as turn_module
from app.conversations.debounce import DUE_KEY, inflight_key, pending_key
from app.conversations.retry import (
    MAX_TURN_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    TURN_DLQ_KEY,
)
from app.conversations.turn import (
    TURN_FAILED_MESSAGE,
    TurnFailed,
    process_turn,
    send_failure_notice,
)
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
HEARTBEAT_PREFIX = "lock-heartbeat:"

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")


class SendFailed(Exception):
    """What a channel provider being down looks like to the turn."""


class Boom(Exception):
    pass


class RecordingRuntime:
    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[str] = []
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return self.reply


class FlakyAdapter:
    """Records every send; fails the ones it is told to.

    `fail_sends` holds 1-based indices of the send attempts that raise;
    `fail_always` makes every send raise; `fail_for` restricts failures to the
    listed conversations.
    """

    def __init__(
        self,
        *,
        fail_sends: set[int] | None = None,
        fail_always: bool = False,
        fail_for: set[uuid.UUID] | None = None,
    ) -> None:
        self.fail_sends = fail_sends or set()
        self.fail_always = fail_always
        self.fail_for = fail_for
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
        targeted = self.fail_for is None or message.conversation_id in self.fail_for
        if targeted and (self.fail_always or len(self.attempts) in self.fail_sends):
            raise SendFailed("provider returned 503")
        self.delivered.append((message.conversation_id, message.content))

    def sent_to(self, conversation_id: uuid.UUID) -> list[str]:
        return [content for cid, content in self.delivered if cid == conversation_id]

    def tried_for(self, conversation_id: uuid.UUID) -> list[str]:
        return [content for cid, content in self.attempts if cid == conversation_id]


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, *, humanize: bool = False) -> tuple[dict, str]:
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


async def _conversation(client, fake_redis, *, humanize: bool = False, text: str = "Oi"):
    headers, channel_id = await _setup(client, humanize=humanize)
    message_id, conversation_id = await _send(client, headers, channel_id, text)
    await _drain_jobs(fake_redis)
    return headers, channel_id, message_id, conversation_id


async def _replies(db_session, conversation_id: uuid.UUID) -> list[str]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT,
        )
        .order_by(Message.seq)
    )
    return [row.content for row in result.scalars().all()]


async def _inbound(db_session, conversation_id):
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _dead_letters(fake_redis) -> list[dict]:
    return [json.loads(raw) for raw in await fake_redis.lrange(TURN_DLQ_KEY, 0, -1)]


def _attempts_key(conversation_id) -> str:
    return retry.attempts_key(conversation_id)


def _heartbeats() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(HEARTBEAT_PREFIX) and not task.done()
    ]


def _stub_adapter(monkeypatch, adapter: FlakyAdapter) -> FlakyAdapter:
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    return adapter


def _no_sleep(monkeypatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)


# --- through `tick` ----------------------------------------------------------


async def test_send_failing_every_time_retries_then_dead_letters_once(
    client, fake_redis, db_session, monkeypatch
):
    _, _, message_id, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_always=True))
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5
    first_retry = now + RETRY_BACKOFF_SECONDS[0]
    second_retry = first_retry + RETRY_BACKOFF_SECONDS[1]

    with structlog.testing.capture_logs() as logs:
        # Attempt 1 fails: back in pending, due after the first backoff.
        assert await tick(fake_redis, factory, runtime, now=now) == []
        assert len(runtime.calls) == 1
        assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) == first_retry
        pending = await fake_redis.lrange(pending_key(conversation_id), 0, -1)
        assert [uuid.UUID(item) for item in pending] == [message_id]
        assert await fake_redis.exists(inflight_key(conversation_id)) == 0
        assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0

        # Not before the backoff.
        assert await tick(fake_redis, factory, runtime, now=first_retry - 0.5) == []
        assert len(runtime.calls) == 1

        # Attempt 2 fails: due after the second backoff.
        assert await tick(fake_redis, factory, runtime, now=first_retry) == []
        assert len(runtime.calls) == 2
        assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) == second_retry
        assert await tick(fake_redis, factory, runtime, now=second_retry - 0.5) == []
        assert len(runtime.calls) == 2
        assert await _dead_letters(fake_redis) == []

        # Attempt 3 fails: dead-lettered, one error message stored.
        assert await tick(fake_redis, factory, runtime, now=second_retry) == []
        assert len(runtime.calls) == MAX_TURN_ATTEMPTS

    entries = await _dead_letters(fake_redis)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["conversation_id"] == str(conversation_id)
    assert entry["message_ids"] == [str(message_id)]
    assert entry["partly_delivered"] is False
    assert entry["attempts"] == MAX_TURN_ATTEMPTS
    assert "SendFailed" in entry["error"]
    assert isinstance(entry["failed_at"], float)

    assert await _replies(db_session, conversation_id) == [TURN_FAILED_MESSAGE]
    # Three reply attempts plus the (also failing) notice; nothing delivered.
    assert adapter.tried_for(conversation_id) == ["ok", "ok", "ok", TURN_FAILED_MESSAGE]
    assert adapter.delivered == []

    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.exists(pending_key(conversation_id)) == 0
    assert await fake_redis.exists(_attempts_key(conversation_id)) == 0
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0

    retries = [entry for entry in logs if entry["event"] == "scheduler.turn_retry"]
    assert [entry["attempt"] for entry in retries] == [1, 2]
    dead = [entry for entry in logs if entry["event"] == "scheduler.turn_dead_lettered"]
    assert len(dead) == 1 and dead[0]["log_level"] == "error"

    # Nothing after: no more turns, no second error message.
    assert await tick(fake_redis, factory, runtime, now=second_retry + 1000) == []
    assert len(runtime.calls) == MAX_TURN_ATTEMPTS
    assert await _replies(db_session, conversation_id) == [TURN_FAILED_MESSAGE]
    assert len(await _dead_letters(fake_redis)) == 1


async def test_fails_once_then_works(client, fake_redis, db_session, monkeypatch):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_sends={1}))
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5

    assert await tick(fake_redis, factory, runtime, now=now) == []
    assert await fake_redis.get(_attempts_key(conversation_id)) == "1"

    retry_at = now + RETRY_BACKOFF_SECONDS[0]
    assert await tick(fake_redis, factory, runtime, now=retry_at) == [conversation_id]

    assert runtime.calls == ["Oi", "Oi"]
    assert adapter.sent_to(conversation_id) == ["ok"]
    assert await _replies(db_session, conversation_id) == ["ok"]
    assert await fake_redis.exists(_attempts_key(conversation_id)) == 0
    assert await _dead_letters(fake_redis) == []
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0


async def test_second_bubble_fails_keeps_first_and_does_not_retry(
    client, fake_redis, db_session, monkeypatch
):
    _, _, message_id, conversation_id = await _conversation(
        client, fake_redis, humanize=True
    )
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_sends={2}))
    _no_sleep(monkeypatch)
    runtime = RecordingRuntime(MULTI_PART_REPLY)
    factory = _session_factory(db_session)
    now = time.time() + 5

    with structlog.testing.capture_logs() as logs:
        assert await tick(fake_redis, factory, runtime, now=now) == []

    assert adapter.sent_to(conversation_id) == [PARTS[0]]
    assert adapter.tried_for(conversation_id) == PARTS[:2]  # part 3 never tried
    assert await _replies(db_session, conversation_id) == [PARTS[0]]

    entries = await _dead_letters(fake_redis)
    assert len(entries) == 1
    assert entries[0]["partly_delivered"] is True
    assert entries[0]["message_ids"] == [str(message_id)]
    assert entries[0]["attempts"] == 1
    assert [entry["event"] for entry in logs if entry["event"].startswith("scheduler.turn_")] == [
        "scheduler.turn_dead_lettered"
    ]

    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.exists(pending_key(conversation_id)) == 0
    assert await fake_redis.exists(_attempts_key(conversation_id)) == 0

    # No retry, ever: the first bubble is never sent twice.
    assert await tick(fake_redis, factory, runtime, now=now + 1000) == []
    assert len(runtime.calls) == 1
    assert adapter.tried_for(conversation_id) == PARTS[:2]
    assert TURN_FAILED_MESSAGE not in await _replies(db_session, conversation_id)


async def test_failure_in_final_commit_after_send_is_not_retried(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())
    runtime = RecordingRuntime()
    original_commit = db_session.commit
    commits = {"after_send": 0}

    # The turn commits before its send too (no transaction across the send);
    # the one that fails here is the first commit after the reply went out.
    async def failing_commit():
        if adapter.delivered:
            commits["after_send"] += 1
            if commits["after_send"] == 1:
                raise Boom("database went away")
        return await original_commit()

    monkeypatch.setattr(db_session, "commit", failing_commit)
    now = time.time() + 5

    assert await tick(fake_redis, _session_factory(db_session), runtime, now=now) == []

    assert adapter.sent_to(conversation_id) == ["ok"]
    entries = await _dead_letters(fake_redis)
    assert len(entries) == 1 and entries[0]["partly_delivered"] is True
    assert await tick(fake_redis, _session_factory(db_session), runtime, now=now + 1000) == []
    assert adapter.tried_for(conversation_id) == ["ok"]
    assert TURN_FAILED_MESSAGE not in await _replies(db_session, conversation_id)


async def test_failing_conversation_does_not_hold_up_a_healthy_one(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, failing_id = await _conversation(client, fake_redis, text="falha")
    _, _, _, healthy_id = await _conversation(client, fake_redis, text="tudo bem")
    adapter = _stub_adapter(
        monkeypatch, FlakyAdapter(fail_always=True, fail_for={failing_id})
    )
    runtime = RecordingRuntime()
    now = time.time() + 5
    # The failing conversation comes first in the tick.
    await debounce.schedule(fake_redis, failing_id, now - 10)
    await debounce.schedule(fake_redis, healthy_id, now - 5)

    processed = await tick(fake_redis, _session_factory(db_session), runtime, now=now)

    assert processed == [healthy_id]
    assert runtime.calls == ["falha", "tudo bem"]
    assert adapter.sent_to(healthy_id) == ["ok"]
    assert await _replies(db_session, healthy_id) == ["ok"]
    assert await fake_redis.zscore(DUE_KEY, str(failing_id)) == now + RETRY_BACKOFF_SECONDS[0]
    assert await fake_redis.zscore(DUE_KEY, str(healthy_id)) is None


async def test_message_arriving_during_the_wait_is_answered_with_the_batch(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_sends={1}))
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5

    assert await tick(fake_redis, factory, runtime, now=now) == []

    await _send(client, headers, channel_id, "alô?")
    await _drain_jobs(fake_redis)

    assert await tick(fake_redis, factory, runtime, now=now + RETRY_BACKOFF_SECONDS[0]) == [
        conversation_id
    ]
    assert runtime.calls == ["Oi", "Oi\nalô?"]
    assert adapter.sent_to(conversation_id) == ["ok"]
    assert await _replies(db_session, conversation_id) == ["ok"]
    assert await fake_redis.exists(pending_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None


async def test_runtime_error_is_retried_and_lock_released(
    client, fake_redis, db_session, monkeypatch
):
    _, _, message_id, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())

    class FailingRuntime:
        async def run(self, conversation, input_text: str) -> str:
            raise Boom("tool exploded")

    now = time.time() + 5
    assert await tick(fake_redis, _session_factory(db_session), FailingRuntime(), now=now) == []

    assert adapter.attempts == []
    assert _heartbeats() == []
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    pending = await fake_redis.lrange(pending_key(conversation_id), 0, -1)
    assert [uuid.UUID(item) for item in pending] == [message_id]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) == now + RETRY_BACKOFF_SECONDS[0]


async def test_refused_turn_clears_the_counter_and_is_not_retried(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())
    await retry.record_failure(fake_redis, conversation_id)
    conversation = await db_session.get(Conversation, conversation_id)
    conversation.state = ConversationState.HUMAN_HANDOFF
    await db_session.commit()
    runtime = RecordingRuntime()

    processed = await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    assert processed == [conversation_id]
    assert runtime.calls == []
    assert adapter.attempts == []
    assert await fake_redis.exists(_attempts_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await _dead_letters(fake_redis) == []


async def test_broken_failure_handling_does_not_stop_the_tick(
    client, fake_redis, db_session, monkeypatch
):
    _, _, message_id, failing_id = await _conversation(client, fake_redis, text="falha")
    _, _, _, healthy_id = await _conversation(client, fake_redis, text="tudo bem")
    _stub_adapter(monkeypatch, FlakyAdapter(fail_always=True, fail_for={failing_id}))

    async def broken_record_failure(redis, conversation_id):
        raise ConnectionError("redis blip")

    monkeypatch.setattr(retry, "record_failure", broken_record_failure)
    now = time.time() + 5
    await debounce.schedule(fake_redis, failing_id, now - 10)
    await debounce.schedule(fake_redis, healthy_id, now - 5)

    processed = await tick(fake_redis, _session_factory(db_session), RecordingRuntime(), now=now)

    assert processed == [healthy_id]
    assert await fake_redis.exists(lock.lock_key(failing_id)) == 0
    # The batch stays in flight under the claim: the claim timeout brings it back.
    inflight = await fake_redis.lrange(inflight_key(failing_id), 0, -1)
    assert [uuid.UUID(item) for item in inflight] == [message_id]
    assert await fake_redis.zscore(DUE_KEY, str(failing_id)) == now + debounce.CLAIM_TIMEOUT_SECONDS


async def test_slow_failure_backs_off_from_the_failure_not_the_claim(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch, FlakyAdapter(fail_always=True))
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    # The tick claimed 30 s ago, and the turn fails only now: much longer than
    # the first backoff.
    claimed_at = time.time() - 30
    await debounce.schedule(fake_redis, conversation_id, claimed_at - 1)

    before_failure = time.time()
    assert await tick(fake_redis, factory, runtime, now=claimed_at) == []
    assert len(runtime.calls) == 1

    due = await fake_redis.zscore(DUE_KEY, str(conversation_id))
    assert due >= before_failure + RETRY_BACKOFF_SECONDS[0]
    assert due <= time.time() + RETRY_BACKOFF_SECONDS[0]
    # A worker looking right away does not run the retry yet.
    assert await tick(fake_redis, factory, runtime, now=time.time()) == []
    assert len(runtime.calls) == 1


async def test_summary_step_that_raises_keeps_the_sent_reply(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)

    async def broken_summary(session, conversation, runtime):
        raise Boom("summarizer exploded")

    monkeypatch.setattr(turn_module, "_maybe_summarize", broken_summary)
    now = time.time() + 5

    assert await tick(fake_redis, factory, runtime, now=now) == [conversation_id]

    assert adapter.sent_to(conversation_id) == ["ok"]
    assert await _replies(db_session, conversation_id) == ["ok"]
    assert await _dead_letters(fake_redis) == []
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.exists(debounce.claim_key(conversation_id)) == 0

    # Never answered a second time.
    assert await tick(fake_redis, factory, runtime, now=now + 1000) == []
    assert adapter.tried_for(conversation_id) == ["ok"]
    assert await _replies(db_session, conversation_id) == ["ok"]


async def test_failing_commit_after_the_summary_keeps_the_sent_reply(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())
    runtime = RecordingRuntime()
    original_commit = db_session.commit
    commits = {"after_send": 0}

    # Commits after the send: the first stores the reply, the second the
    # summary. The turn's commits before the send are not counted.
    async def failing_second_commit():
        if adapter.delivered:
            commits["after_send"] += 1
            if commits["after_send"] == 2:
                raise Boom("database went away")
        return await original_commit()

    monkeypatch.setattr(db_session, "commit", failing_second_commit)
    now = time.time() + 5

    assert await tick(fake_redis, _session_factory(db_session), runtime, now=now) == [
        conversation_id
    ]

    assert adapter.sent_to(conversation_id) == ["ok"]
    assert await _replies(db_session, conversation_id) == ["ok"]
    assert await _dead_letters(fake_redis) == []
    assert await tick(fake_redis, _session_factory(db_session), runtime, now=now + 1000) == []
    assert adapter.tried_for(conversation_id) == ["ok"]


# --- `process_turn` ----------------------------------------------------------


async def test_process_turn_wraps_errors_in_turn_failed(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch, FlakyAdapter())
    messages = await _inbound(db_session, conversation_id)

    class FailingRuntime:
        async def run(self, conversation, input_text: str) -> str:
            raise Boom("model down")

    with pytest.raises(TurnFailed) as caught:
        await process_turn(db_session, conversation_id, messages, FailingRuntime())

    assert caught.value.delivered_parts == 0
    assert isinstance(caught.value.__cause__, Boom)
    assert await _replies(db_session, conversation_id) == []


async def test_process_turn_later_bubble_failure_stops_the_reply(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis, humanize=True)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_sends={2}))
    _no_sleep(monkeypatch)
    messages = await _inbound(db_session, conversation_id)

    with pytest.raises(TurnFailed) as caught:
        await process_turn(
            db_session, conversation_id, messages, RecordingRuntime(MULTI_PART_REPLY)
        )

    assert caught.value.delivered_parts == 1
    assert isinstance(caught.value.__cause__, SendFailed)
    assert adapter.tried_for(conversation_id) == PARTS[:2]
    assert await _replies(db_session, conversation_id) == [PARTS[0]]


async def test_process_turn_first_send_failure_delivers_nothing(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch, FlakyAdapter(fail_always=True))
    messages = await _inbound(db_session, conversation_id)

    with pytest.raises(TurnFailed) as caught:
        await process_turn(db_session, conversation_id, messages, RecordingRuntime())

    assert caught.value.delivered_parts == 0
    assert isinstance(caught.value.__cause__, SendFailed)
    assert await _replies(db_session, conversation_id) == []


# --- `send_failure_notice` -----------------------------------------------------


async def test_failure_notice_is_stored_even_when_its_send_fails(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter(fail_always=True))

    assert await send_failure_notice(db_session, conversation_id) is True

    assert adapter.tried_for(conversation_id) == [TURN_FAILED_MESSAGE]
    assert await _replies(db_session, conversation_id) == [TURN_FAILED_MESSAGE]


async def test_failure_notice_sent_when_channel_works(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())

    assert await send_failure_notice(db_session, conversation_id) is True
    assert adapter.sent_to(conversation_id) == [TURN_FAILED_MESSAGE]


async def test_failure_notice_skipped_outside_active_ai(
    client, fake_redis, db_session, monkeypatch
):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch, FlakyAdapter())
    conversation = await db_session.get(Conversation, conversation_id)
    conversation.state = ConversationState.HUMAN_HANDOFF
    await db_session.commit()

    assert await send_failure_notice(db_session, conversation_id) is False
    assert await send_failure_notice(db_session, uuid.uuid4()) is False
    assert adapter.attempts == []
    assert await _replies(db_session, conversation_id) == []


async def test_failure_notice_never_raises(client, fake_redis, db_session, monkeypatch):
    _, _, _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch, FlakyAdapter())

    async def broken_commit():
        raise Boom("database went away")

    monkeypatch.setattr(db_session, "commit", broken_commit)
    assert await send_failure_notice(db_session, conversation_id) is False


# --- primitives --------------------------------------------------------------


async def test_interface_constants():
    assert MAX_TURN_ATTEMPTS == 3
    assert RETRY_BACKOFF_SECONDS == (2.0, 8.0)
    assert TURN_DLQ_KEY == "harness:dlq:turns"
    assert TURN_FAILED_MESSAGE == "Desculpe, tive um problema técnico. Pode tentar novamente?"
    failed = TurnFailed(2)
    assert isinstance(failed, Exception) and failed.delivered_parts == 2


async def test_counter_counts_expires_and_clears(fake_redis):
    cid = uuid.uuid4()
    assert await retry.record_failure(fake_redis, cid) == 1
    assert await retry.record_failure(fake_redis, str(cid)) == 2
    ttl = await fake_redis.ttl(_attempts_key(cid))
    assert 0 < ttl <= 3600

    await retry.clear(fake_redis, cid)
    assert await fake_redis.exists(_attempts_key(cid)) == 0
    assert await retry.record_failure(fake_redis, cid) == 1
    await retry.clear(fake_redis, uuid.uuid4())  # nothing to clear: no error


async def test_dead_letter_entry_format(fake_redis):
    cid = uuid.uuid4()
    ids = [uuid.uuid4(), uuid.uuid4()]
    before = time.time()

    await retry.dead_letter(
        fake_redis, cid, ids, error="SendFailed: 503", partly_delivered=False, attempts=3
    )
    await retry.dead_letter(
        fake_redis, str(cid), [str(ids[0])], error="x", partly_delivered=True, attempts=1
    )

    first, second = await _dead_letters(fake_redis)
    assert set(first) == {
        "conversation_id",
        "message_ids",
        "error",
        "partly_delivered",
        "attempts",
        "failed_at",
    }
    assert first["conversation_id"] == str(cid)
    assert first["message_ids"] == [str(mid) for mid in ids]
    assert first["error"] == "SendFailed: 503"
    assert first["partly_delivered"] is False
    assert first["attempts"] == 3
    assert before <= first["failed_at"] <= time.time()
    assert second["partly_delivered"] is True and second["message_ids"] == [str(ids[0])]
