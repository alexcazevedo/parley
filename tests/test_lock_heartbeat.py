"""The lock holds for the whole turn (PRODUCT.md E3.5).

While `process_turn` runs, `scheduler.tick` renews the conversation lock (and
pushes the claim out) on a heartbeat; `process_turn` asks "is the lock still
ours?" before every send and before its commit, and a turn that lost it rolls
back, sends nothing more and returns `LockLost`.

TTL and renew interval are monkeypatched to fractions of a second, and the
runtimes really sleep, so a turn outlives its TTL many times over in well
under a second. Each `tick` gets a fresh session from the test engine: two
ticks run concurrently here, and one `AsyncSession` cannot serve both.
"""

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.conversations import debounce, lock
from app.conversations import turn as turn_module
from app.conversations.debounce import DUE_KEY, inflight_key, pending_key
from app.conversations.turn import LockLost, process_turn
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers import scheduler
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
TTL_MS = 300
INTERVAL = 0.05
HEARTBEAT_PREFIX = "lock-heartbeat:"

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")


@pytest.fixture
def short_lock(monkeypatch):
    """A 300 ms lock renewed every 50 ms.

    `acquire`'s default `ttl_ms` is bound when the function is defined, so it
    is patched too: that keeps the reproduction honest against a scheduler
    that does not pass the TTL explicitly.
    """
    monkeypatch.setattr(lock, "LOCK_TTL_MS", TTL_MS)
    monkeypatch.setattr(lock, "LOCK_RENEW_INTERVAL_SECONDS", INTERVAL)
    monkeypatch.setattr(lock.acquire, "__defaults__", (TTL_MS,))
    if hasattr(lock, "renew"):
        monkeypatch.setattr(lock.renew, "__defaults__", (TTL_MS,))


class SleepyRuntime:
    """Takes `delay` seconds per call (a slow model loop) and records calls."""

    def __init__(self, delay: float, reply: str = "ok", during=None) -> None:
        self.delay = delay
        self.reply = reply
        self.during = during
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        self.started.set()
        if self.during is not None:
            await self.during()
        await asyncio.sleep(self.delay)
        return self.reply


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        self.sent.append(message.content)


def _fresh_sessions(test_engine):
    maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

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


async def _conversation(client, fake_redis, *, humanize: bool = False):
    headers, channel_id = await _setup(client, humanize=humanize)
    message_id, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)
    return message_id, conversation_id


async def _replies(test_engine, conversation_id: uuid.UUID) -> list[str]:
    async with _fresh_sessions(test_engine)() as session:
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.ASSISTANT,
            )
            .order_by(Message.seq)
        )
        return [row.content for row in result.scalars().all()]


def _heartbeats() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(HEARTBEAT_PREFIX) and not task.done()
    ]


def _stub_adapter(monkeypatch) -> RecordingAdapter:
    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    return adapter


# --- through `tick` ----------------------------------------------------------


async def test_turn_longer_than_ttl_keeps_lock_and_blocks_second_turn(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    runtime = SleepyRuntime(delay=0.8)  # more than two TTLs
    factory = _fresh_sessions(test_engine)
    key = lock.lock_key(conversation_id)
    now = time.time() + 1  # past the debounce window

    first = asyncio.create_task(tick(fake_redis, factory, runtime, now=now))
    await runtime.started.wait()
    token = await fake_redis.get(key)
    assert token is not None

    await asyncio.sleep(0.5)  # well past the original TTL
    assert await fake_redis.get(key) == token
    assert _heartbeats()

    # Another worker whose clock says the claim timed out long ago: it claims
    # the due entry, but the lock is still held, so no second turn starts.
    assert await tick(fake_redis, factory, runtime, now=now + 1000) == []
    assert runtime.calls == ["Oi"]

    assert await first == [conversation_id]
    assert runtime.calls == ["Oi"]
    assert adapter.sent == ["ok"]
    assert await _replies(test_engine, conversation_id) == ["ok"]
    assert _heartbeats() == []
    assert await fake_redis.exists(key) == 0
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None


async def test_heartbeat_pushes_the_claim_out(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch)
    runtime = SleepyRuntime(delay=0.3)
    claimed_at = time.time() - 59.9  # claim would time out 0.1 s from now
    await debounce.schedule(fake_redis, conversation_id, claimed_at)
    seen: list[float] = []

    async def watch_score():
        await runtime.started.wait()
        await asyncio.sleep(0.2)
        seen.append(await fake_redis.zscore(DUE_KEY, str(conversation_id)))

    watcher = asyncio.create_task(watch_score())
    processed = await tick(
        fake_redis, _fresh_sessions(test_engine), runtime, now=claimed_at
    )
    await watcher

    assert processed == [conversation_id]
    assert seen and seen[0] > time.time() + 50  # extended from real time


async def test_lock_replaced_mid_turn_returns_lock_lost_and_stays_silent(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    message_id, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    key = lock.lock_key(conversation_id)

    async def steal():
        await fake_redis.set(key, "other-worker", px=60_000)

    runtime = SleepyRuntime(delay=0.1, during=steal)
    outcomes: list = []
    original = turn_module.process_turn

    async def recording_process_turn(*args, **kwargs):
        outcomes.append(await original(*args, **kwargs))
        return outcomes[-1]

    monkeypatch.setattr(scheduler, "process_turn", recording_process_turn)
    now = time.time() + 1  # past the debounce window

    with structlog.testing.capture_logs() as logs:
        processed = await tick(fake_redis, _fresh_sessions(test_engine), runtime, now=now)

    assert processed == []
    assert outcomes == [LockLost(0)]
    assert adapter.sent == []
    assert await _replies(test_engine, conversation_id) == []
    # The batch belongs to the new holder: in flight, untouched.
    inflight = await fake_redis.lrange(inflight_key(conversation_id), 0, -1)
    assert [uuid.UUID(item) for item in inflight] == [message_id]
    assert await fake_redis.exists(pending_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None
    assert await fake_redis.get(key) == "other-worker"  # not released
    assert _heartbeats() == []
    events = [entry for entry in logs if entry["event"] == "scheduler.lock_lost"]
    assert events and events[0]["log_level"] == "warning"


async def test_lock_expiring_during_event_loop_stall_is_lost(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    message_id, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)

    class StallingRuntime:
        async def run(self, conversation, input_text: str) -> str:
            time.sleep(0.6)  # blocks the loop for two TTLs: no heartbeat can run
            return "ok"

    processed = await tick(
        fake_redis, _fresh_sessions(test_engine), StallingRuntime(), now=time.time() + 1
    )

    assert processed == []
    assert adapter.sent == []
    assert await _replies(test_engine, conversation_id) == []
    inflight = await fake_redis.lrange(inflight_key(conversation_id), 0, -1)
    assert [uuid.UUID(item) for item in inflight] == [message_id]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None
    assert _heartbeats() == []


async def test_redis_unreachable_for_a_whole_ttl_loses_the_lock(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)

    async def unreachable(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(lock, "renew", unreachable)
    processed = await tick(
        fake_redis, _fresh_sessions(test_engine), SleepyRuntime(delay=0.45), now=time.time() + 1
    )

    assert processed == []
    assert adapter.sent == []
    assert await _replies(test_engine, conversation_id) == []
    assert await fake_redis.exists(inflight_key(conversation_id)) == 1
    assert _heartbeats() == []


async def test_short_redis_outage_keeps_the_lock(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    original_renew = lock.renew
    outage_until = time.monotonic() + 0.12  # well under the 300 ms TTL

    async def flaky(*args, **kwargs):
        if time.monotonic() < outage_until:
            raise ConnectionError("redis blip")
        return await original_renew(*args, **kwargs)

    monkeypatch.setattr(lock, "renew", flaky)
    processed = await tick(
        fake_redis, _fresh_sessions(test_engine), SleepyRuntime(delay=0.6), now=time.time() + 1
    )

    assert processed == [conversation_id]
    assert adapter.sent == ["ok"]
    assert await _replies(test_engine, conversation_id) == ["ok"]
    assert _heartbeats() == []


async def test_heartbeat_gone_after_failed_turn(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch)

    class Boom(Exception):
        pass

    class FailingRuntime:
        async def run(self, conversation, input_text: str) -> str:
            await asyncio.sleep(0.15)
            raise Boom

    # PRODUCT.md E3.2 (ticket 04): a failed turn no longer leaves `tick`; its
    # batch goes back to pending for a retry instead of waiting in flight.
    assert (
        await tick(fake_redis, _fresh_sessions(test_engine), FailingRuntime(), now=time.time() + 1)
        == []
    )

    assert _heartbeats() == []
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0
    assert await fake_redis.exists(inflight_key(conversation_id)) == 0
    assert await fake_redis.llen(pending_key(conversation_id)) == 1  # comes back later


async def test_turn_ending_while_a_renewal_is_in_flight(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    original_renew = lock.renew
    stuck = asyncio.Event()

    async def hanging_in_heartbeat(*args, **kwargs):
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith(HEARTBEAT_PREFIX):
            stuck.set()
            await asyncio.Event().wait()  # never answers
        return await original_renew(*args, **kwargs)

    monkeypatch.setattr(lock, "renew", hanging_in_heartbeat)
    runtime = SleepyRuntime(delay=0.1)
    processed = await asyncio.wait_for(
        tick(fake_redis, _fresh_sessions(test_engine), runtime, now=time.time() + 1), timeout=2
    )

    assert stuck.is_set()
    assert processed == [conversation_id]
    assert adapter.sent == ["ok"]
    assert _heartbeats() == []
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0


async def test_lock_lost_during_summary_keeps_reply_and_never_answers_twice(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    message_id, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    key = lock.lock_key(conversation_id)

    async def summary_that_loses_the_lock(session, conversation, runtime):
        await fake_redis.set(key, "other-worker", px=60_000)

    monkeypatch.setattr(turn_module, "_maybe_summarize", summary_that_loses_the_lock)
    factory = _fresh_sessions(test_engine)
    runtime = SleepyRuntime(delay=0)
    now = time.time() + 1

    with structlog.testing.capture_logs() as logs:
        await tick(fake_redis, factory, runtime, now=now)

    assert adapter.sent == ["ok"]
    assert await _replies(test_engine, conversation_id) == ["ok"]  # the sent reply stays
    # The answered batch is out of flight; the lock and due entry are the new holder's.
    inflight = await fake_redis.lrange(inflight_key(conversation_id), 0, -1)
    assert str(message_id) not in inflight
    assert await fake_redis.get(key) == "other-worker"
    assert any(entry["event"] == "scheduler.lock_lost" for entry in logs)

    # The other worker goes away; once its lock and the claim expire, the
    # conversation comes back and has nothing left to answer.
    await fake_redis.delete(key, debounce.claim_key(conversation_id))
    assert await tick(fake_redis, factory, runtime, now=now + 1000) == []
    assert runtime.calls == ["Oi"]
    assert adapter.sent == ["ok"]
    assert await _replies(test_engine, conversation_id) == ["ok"]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None


async def test_message_pushed_mid_turn_is_not_claimed_by_another_worker(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch)
    factory = _fresh_sessions(test_engine)
    outcomes: list = []

    async def push_then_second_worker():
        await debounce.push_pending(
            fake_redis, conversation_id, uuid.uuid4(), due_at=time.time() + DEBOUNCE_SECONDS
        )
        # A second worker, past that message's debounce window.
        outcomes.append(await tick(fake_redis, factory, SleepyRuntime(delay=0), now=now + 5))

    runtime = SleepyRuntime(delay=0, during=push_then_second_worker)
    now = time.time() + 1
    with structlog.testing.capture_logs() as logs:
        await tick(fake_redis, factory, runtime, now=now)

    assert outcomes == [[]]
    # It never got as far as the lock: the claim stopped it.
    assert not any(entry["event"] == "scheduler.lock_busy" for entry in logs)
    # The interrupted batch and the new message wait together, due now.
    assert await fake_redis.llen(pending_key(conversation_id)) == 2
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) <= time.time()


async def test_heartbeat_never_raises_into_tick(
    client, fake_redis, test_engine, short_lock, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    _stub_adapter(monkeypatch)

    async def broken_extend(*args, **kwargs):
        raise RuntimeError("extend failed")

    monkeypatch.setattr(debounce, "extend_claim", broken_extend)
    processed = await tick(
        fake_redis, _fresh_sessions(test_engine), SleepyRuntime(delay=0.4), now=time.time() + 1
    )

    assert processed == [conversation_id]
    assert _heartbeats() == []


# --- `process_turn` with a scripted ownership check ---------------------------


class ScriptedOwnership:
    def __init__(self, answers: list[bool] | None = None, *, raises: Exception | None = None):
        self.answers = list(answers or [])
        self.raises = raises
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if self.answers else True


async def _inbound(db_session, conversation_id):
    result = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
    )
    return list(result.scalars().all())


async def _no_sleep(monkeypatch):
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)


class FixedRuntime:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        return self.reply


async def test_lost_between_bubbles_keeps_sent_parts_only(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis, humanize=True)
    adapter = _stub_adapter(monkeypatch)
    await _no_sleep(monkeypatch)
    messages = await _inbound(db_session, conversation_id)
    # mark_read, part 1, then lost before part 2.
    check = ScriptedOwnership([True, True, False])

    result = await process_turn(
        db_session,
        conversation_id,
        messages,
        FixedRuntime(MULTI_PART_REPLY),
        ownership_check=check,
    )

    assert result == LockLost(1)
    assert adapter.sent == [PARTS[0]]
    assert await _replies(test_engine, conversation_id) == [PARTS[0]]


async def test_lost_before_summary_commit_keeps_the_sent_reply(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    messages = await _inbound(db_session, conversation_id)
    # Owned before the send, gone by the summary's commit. The reply was
    # committed right after its send (ticket 07): it is delivered, so it stays.
    check = ScriptedOwnership([True, False])

    result = await process_turn(
        db_session, conversation_id, messages, FixedRuntime("ok"), ownership_check=check
    )

    assert isinstance(result, Message) and result.content == "ok"
    assert check.calls == 2
    assert adapter.sent == ["ok"]
    assert await _replies(test_engine, conversation_id) == ["ok"]


async def test_failing_ownership_check_counts_as_lost(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis)
    adapter = _stub_adapter(monkeypatch)
    messages = await _inbound(db_session, conversation_id)
    check = ScriptedOwnership(raises=RuntimeError("probe broke"))

    result = await process_turn(
        db_session, conversation_id, messages, FixedRuntime("ok"), ownership_check=check
    )

    assert result == LockLost(0)
    assert adapter.sent == []
    assert await _replies(test_engine, conversation_id) == []


async def test_owned_turn_asks_at_each_checkpoint(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    _, conversation_id = await _conversation(client, fake_redis, humanize=True)
    adapter = _stub_adapter(monkeypatch)
    await _no_sleep(monkeypatch)
    messages = await _inbound(db_session, conversation_id)
    check = ScriptedOwnership()

    result = await process_turn(
        db_session,
        conversation_id,
        messages,
        FixedRuntime(MULTI_PART_REPLY),
        ownership_check=check,
    )

    assert isinstance(result, Message)
    assert adapter.sent == PARTS
    # mark_read + one per bubble + the final commit.
    assert check.calls == 1 + len(PARTS) + 1


# --- primitives --------------------------------------------------------------


async def test_renew_interface_constants():
    assert lock.LOCK_RENEW_INTERVAL_SECONDS == 20.0
    assert lock.LOCK_TTL_MS == 60_000
    assert LockLost(2).delivered_parts == 2


async def test_renew_with_wrong_token_does_not_extend(fake_redis):
    cid = uuid.uuid4()
    key = lock.lock_key(cid)
    token = await lock.acquire(fake_redis, cid, ttl_ms=1_000)
    assert token is not None

    assert await lock.renew(fake_redis, cid, "not-mine", ttl_ms=100_000) is False
    assert await fake_redis.pttl(key) <= 1_000
    assert await fake_redis.get(key) == token

    assert await lock.renew(fake_redis, cid, token, ttl_ms=100_000) is True
    assert await fake_redis.pttl(key) > 1_000


async def test_renew_missing_lock_returns_false_and_creates_nothing(fake_redis):
    cid = uuid.uuid4()
    assert await lock.renew(fake_redis, cid, "whatever") is False
    assert await fake_redis.exists(lock.lock_key(cid)) == 0


async def test_extend_claim_moves_existing_entry_only(fake_redis):
    cid = uuid.uuid4()
    await debounce.schedule(fake_redis, cid, 100.0)

    await debounce.extend_claim(fake_redis, cid, now=500.0, timeout=30.0)
    assert await fake_redis.zscore(DUE_KEY, str(cid)) == 530.0

    other = uuid.uuid4()
    await debounce.extend_claim(fake_redis, other, now=500.0)
    assert await fake_redis.zscore(DUE_KEY, str(other)) is None
