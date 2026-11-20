"""Recoverable claim (PRODUCT.md E3.3): a conversation claimed by a worker
that then dies is picked up again after a timeout, with its batch intact.

Crashes are simulated by making one step of `scheduler.tick` raise and, where
a real crash would leave the per-conversation lock behind, by skipping the
release and deleting the lock key later (its TTL "expiring"). Every tick gets
an explicit `now`, as in `tests/test_barge_in_scheduler.py`.
"""

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.conversations import debounce, lock
from app.conversations import turn as turn_module
from app.conversations.debounce import DUE_KEY, pending_key
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
# Later than `debounce.CLAIM_TIMEOUT_SECONDS` (60 s) past the crashed tick.
AFTER_TIMEOUT = 60.0 + 1


class Crash(Exception):
    """Stands in for the worker process dying at a given step."""


class RecordingRuntime:
    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[str] = []
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return self.reply


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client) -> tuple[dict, str]:
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
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
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


async def _claim_expires(fake_redis, conversation_id) -> None:
    """The dead claimer's claim key runs out (its TTL is real time; `now` is not)."""
    await fake_redis.delete(debounce.claim_key(conversation_id))


def _ids(raw: list[str]) -> list[uuid.UUID]:
    return [uuid.UUID(item) for item in raw]


def _interleave(monkeypatch, redis, method: str, action) -> list[bool]:
    """Run `action()` once, right after the first `method` read a WATCHing
    transaction pipeline makes — i.e. between its read and its EXEC.
    """
    original_pipeline = redis.pipeline
    fired: list[bool] = []

    def pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        original = getattr(pipe, method)

        def wrapped(*a, **kw):
            result = original(*a, **kw)
            if not pipe.watching or fired:
                return result

            async def run():
                value = await result
                fired.append(True)
                await action()
                return value

            return run()

        setattr(pipe, method, wrapped)
        return pipe

    monkeypatch.setattr(redis, "pipeline", pipeline)
    return fired


# --- crash recovery through `tick` -------------------------------------------


async def test_crash_after_claim_is_picked_up_after_timeout(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id = await _setup(client)
    _, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5

    async def die(*args, **kwargs):
        raise Crash

    original_acquire = lock.acquire
    monkeypatch.setattr(lock, "acquire", die)
    with pytest.raises(Crash):
        await tick(fake_redis, factory, runtime, now=now)
    monkeypatch.setattr(lock, "acquire", original_acquire)

    # Still claimed: another worker inside the timeout leaves it alone.
    assert await tick(fake_redis, factory, runtime, now=now + 1) == []
    assert runtime.calls == []

    await _claim_expires(fake_redis, conversation_id)
    assert await tick(fake_redis, factory, runtime, now=now + AFTER_TIMEOUT) == [conversation_id]
    assert runtime.calls == ["Oi"]
    assert await _replies(db_session, conversation_id) == ["ok"]


async def test_crash_after_drain_answers_same_batch_once(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id = await _setup(client)
    _, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _send(client, headers, channel_id, "tudo bem?")
    await _drain_jobs(fake_redis)
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5

    original_drain = debounce.drain_pending
    original_release = lock.release

    async def drain_then_die(redis, cid):
        await original_drain(redis, cid)
        raise Crash

    async def no_release(*args, **kwargs):  # a dead worker releases nothing
        return False

    monkeypatch.setattr(debounce, "drain_pending", drain_then_die)
    monkeypatch.setattr(lock, "release", no_release)
    with pytest.raises(Crash):
        await tick(fake_redis, factory, runtime, now=now)
    monkeypatch.setattr(debounce, "drain_pending", original_drain)
    monkeypatch.setattr(lock, "release", original_release)

    assert runtime.calls == []
    # The dead worker's lock expires before the claim timeout does.
    await fake_redis.delete(lock.lock_key(conversation_id))

    assert await tick(fake_redis, factory, runtime, now=now + 1) == []
    await _claim_expires(fake_redis, conversation_id)
    assert await tick(fake_redis, factory, runtime, now=now + AFTER_TIMEOUT) == [conversation_id]
    assert runtime.calls == ["Oi\ntudo bem?"]
    assert await _replies(db_session, conversation_id) == ["ok"]

    # Answered once: nothing left anywhere, a later tick does nothing.
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(debounce.inflight_key(conversation_id)) == 0
    assert await tick(fake_redis, factory, runtime, now=now + 2 * AFTER_TIMEOUT) == []
    assert len(runtime.calls) == 1


async def test_batch_left_by_dead_worker_comes_before_newer_messages(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id = await _setup(client)
    _, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)
    now = time.time() + 5

    original_drain = debounce.drain_pending

    async def drain_then_die(redis, cid):
        await original_drain(redis, cid)
        raise Crash

    monkeypatch.setattr(debounce, "drain_pending", drain_then_die)
    with pytest.raises(Crash):
        await tick(fake_redis, factory, runtime, now=now)
    monkeypatch.setattr(debounce, "drain_pending", original_drain)

    # A newer message arrives while the batch sits with the dead worker.
    await _send(client, headers, channel_id, "alô?")
    await _drain_jobs(fake_redis)

    await _claim_expires(fake_redis, conversation_id)
    assert await tick(fake_redis, factory, runtime, now=now + AFTER_TIMEOUT) == [conversation_id]
    assert runtime.calls == ["Oi\nalô?"]


# --- normal operation --------------------------------------------------------


async def test_finished_turn_leaves_no_due_entry_or_inflight(client, fake_redis, db_session):
    headers, channel_id = await _setup(client)
    _, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)
    runtime = RecordingRuntime()

    processed = await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)

    assert processed == [conversation_id]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None
    assert await fake_redis.exists(debounce.inflight_key(conversation_id)) == 0
    assert await fake_redis.exists(pending_key(conversation_id)) == 0
    assert await fake_redis.exists(lock.lock_key(conversation_id)) == 0


async def test_message_pushed_while_turn_runs_gets_its_own_turn(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id = await _setup(client)
    _, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)

    class PushingAdapter:
        """Lands a new message while the reply is being sent (after the
        supersede checkpoint), so it cannot merge into this turn."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, message, contact, channel) -> None:
            self.sent.append(message.content)
            if len(self.sent) == 1:
                await _send(client, headers, channel_id, "mais uma coisa")
                await _drain_jobs(fake_redis)

    adapter = PushingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    runtime = RecordingRuntime()
    factory = _session_factory(db_session)

    assert await tick(fake_redis, factory, runtime, now=time.time() + 5) == [conversation_id]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None
    assert await fake_redis.exists(debounce.inflight_key(conversation_id)) == 0

    assert await tick(fake_redis, factory, runtime, now=time.time() + 10) == [conversation_id]
    assert runtime.calls == ["Oi", "mais uma coisa"]
    assert adapter.sent == ["ok", "ok"]
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is None


async def test_barge_in_merges_old_and_new_in_order(client, fake_redis, db_session):
    headers, channel_id = await _setup(client)
    first_id, conversation_id = await _send(client, headers, channel_id, "Oi")
    await _drain_jobs(fake_redis)
    interrupt_ids: list[uuid.UUID] = []

    class InterruptOnce(RecordingRuntime):
        async def run(self, conversation, input_text):
            if not self.calls:
                interrupt_id, _ = await _send(client, headers, channel_id, "na verdade")
                interrupt_ids.append(interrupt_id)
                await _drain_jobs(fake_redis)
            return await super().run(conversation, input_text)

    runtime = InterruptOnce()
    factory = _session_factory(db_session)

    assert await tick(fake_redis, factory, runtime, now=time.time() + 5) == []
    pending = await fake_redis.lrange(pending_key(conversation_id), 0, -1)
    assert _ids(pending) == [first_id, interrupt_ids[0]]
    assert await fake_redis.exists(debounce.inflight_key(conversation_id)) == 0
    assert await fake_redis.zscore(DUE_KEY, str(conversation_id)) is not None

    assert await tick(fake_redis, factory, runtime, now=time.time() + 6) == [conversation_id]
    assert runtime.calls == ["Oi", "Oi\nna verdade"]
    assert await _replies(db_session, conversation_id) == ["ok"]


# --- debounce primitives -----------------------------------------------------


async def test_claim_timeout_constant():
    assert debounce.CLAIM_TIMEOUT_SECONDS == 60.0
    assert debounce.INFLIGHT_KEY_PREFIX == "harness:inflight:"
    assert debounce.inflight_key("abc") == "harness:inflight:abc"


async def test_claim_moves_score_and_keeps_entry(fake_redis):
    cid = str(uuid.uuid4())
    await debounce.schedule(fake_redis, cid, 100.0)

    assert await debounce.claim(fake_redis, cid, now=99.0) is False  # not due yet
    assert await debounce.claim(fake_redis, cid, now=100.0, timeout=30.0) is True
    assert await fake_redis.zscore(DUE_KEY, cid) == 130.0
    assert await debounce.claim(fake_redis, cid, now=110.0) is False  # still claimed
    assert await debounce.claim(fake_redis, str(uuid.uuid4()), now=110.0) is False  # unknown


async def test_two_concurrent_claims_one_wins(fake_redis):
    cid = str(uuid.uuid4())
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)

    results = await asyncio.gather(
        *[debounce.claim(fake_redis, cid, now=5.0) for _ in range(5)]
    )

    assert sorted(results) == [False, False, False, False, True]
    assert await fake_redis.zscore(DUE_KEY, cid) == 5.0 + debounce.CLAIM_TIMEOUT_SECONDS


async def test_drain_appends_to_inflight_and_returns_all(fake_redis):
    cid = uuid.uuid4()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for mid in (a, b):
        await debounce.push_pending(fake_redis, cid, mid, due_at=1.0)
    assert await debounce.drain_pending(fake_redis, cid) == [a, b]
    assert await fake_redis.exists(pending_key(cid)) == 0

    for mid in (c, a):
        await debounce.push_pending(fake_redis, cid, mid, due_at=1.0)
    assert await debounce.drain_pending(fake_redis, cid) == [a, b, c]
    assert await debounce.drain_pending(fake_redis, cid) == [a, b, c]
    assert _ids(await fake_redis.lrange(debounce.inflight_key(cid), 0, -1)) == [a, b, c, a]


async def test_drain_with_nothing_pending_returns_empty(fake_redis):
    assert await debounce.drain_pending(fake_redis, uuid.uuid4()) == []


async def test_push_during_drain_is_not_lost(fake_redis, monkeypatch):
    cid = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, a, due_at=1.0)

    async def push_b():
        await debounce.push_pending(fake_redis, cid, b, due_at=2.0)

    fired = _interleave(monkeypatch, fake_redis, "lrange", push_b)
    drained = await debounce.drain_pending(fake_redis, cid)

    assert fired
    pending = _ids(await fake_redis.lrange(pending_key(cid), 0, -1))
    assert drained[0] == a
    assert sorted(map(str, drained + pending)) == sorted([str(a), str(b)])


async def test_finish_with_nothing_pending_removes_due_and_inflight(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0)
    await debounce.drain_pending(fake_redis, cid)

    await debounce.finish(fake_redis, cid)

    assert await fake_redis.zscore(DUE_KEY, str(cid)) is None
    assert await fake_redis.exists(debounce.inflight_key(cid)) == 0


async def test_finish_with_pending_schedules_now(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0)
    await debounce.drain_pending(fake_redis, cid)
    await fake_redis.rpush(pending_key(cid), str(uuid.uuid4()))  # no ZADD on purpose

    before = time.time()
    await debounce.finish(fake_redis, cid)

    score = await fake_redis.zscore(DUE_KEY, str(cid))
    assert score is not None and before <= score <= time.time()
    assert await fake_redis.exists(debounce.inflight_key(cid)) == 0
    assert await fake_redis.llen(pending_key(cid)) == 1


async def test_push_during_finish_keeps_due_entry(fake_redis, monkeypatch):
    cid = uuid.uuid4()
    late = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0)
    await debounce.drain_pending(fake_redis, cid)

    async def push_late():
        await debounce.push_pending(fake_redis, cid, late, due_at=time.time() + 0.2)

    # finish reads an empty pending list, then the push lands before EXEC.
    fired = _interleave(monkeypatch, fake_redis, "llen", push_late)
    await debounce.finish(fake_redis, cid)

    assert fired
    assert _ids(await fake_redis.lrange(pending_key(cid), 0, -1)) == [late]
    assert await fake_redis.zscore(DUE_KEY, str(cid)) is not None
    assert await fake_redis.exists(debounce.inflight_key(cid)) == 0


def _after_score_reads(monkeypatch, redis, action, *, times: int) -> list[int]:
    """Run `action()` after each of the first `times` ZSCORE reads, direct or
    inside a pipeline. Returns a one-item list counting every ZSCORE read.
    """
    reads = [0]
    original_zscore = redis.zscore
    original_pipeline = redis.pipeline

    def counted(original):
        def wrapped(*a, **kw):
            result = original(*a, **kw)
            if not asyncio.iscoroutine(result):
                return result  # buffered in a MULTI (returns the pipeline): not a read

            async def run():
                value = await result
                reads[0] += 1
                if reads[0] <= times:
                    await action()
                return value

            return run()

        return wrapped

    def pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        pipe.zscore = counted(pipe.zscore)
        return pipe

    monkeypatch.setattr(redis, "zscore", counted(original_zscore))
    monkeypatch.setattr(redis, "pipeline", pipeline)
    return reads


async def test_claim_key_interface():
    assert debounce.CLAIM_KEY_PREFIX == "harness:claim:"
    assert debounce.claim_key("abc") == "harness:claim:abc"


async def test_claim_lost_to_another_claimer_writes_nothing(fake_redis):
    cid = str(uuid.uuid4())
    await debounce.schedule(fake_redis, cid, 1.0)
    # Another worker won the claim key but has not moved the score yet.
    await fake_redis.set(debounce.claim_key(cid), "1", px=60_000)

    assert await debounce.claim(fake_redis, cid, now=5.0) is False
    assert await fake_redis.zscore(DUE_KEY, cid) == 1.0


async def test_claim_does_not_retry_on_pushes_for_other_conversations(fake_redis, monkeypatch):
    cid = str(uuid.uuid4())
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)

    async def push_elsewhere():
        await debounce.push_pending(fake_redis, uuid.uuid4(), uuid.uuid4(), due_at=2.0)

    reads = _after_score_reads(monkeypatch, fake_redis, push_elsewhere, times=5)
    assert await debounce.claim(fake_redis, cid, now=5.0) is True

    assert reads[0] == 1  # read once: the other conversations' writes do not matter
    assert await fake_redis.zscore(DUE_KEY, cid) == 5.0 + debounce.CLAIM_TIMEOUT_SECONDS


async def test_claim_key_is_gone_after_finish(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0, timeout=30.0)
    assert 0 < await fake_redis.pttl(debounce.claim_key(cid)) <= 30_000

    await debounce.drain_pending(fake_redis, cid)
    await debounce.finish(fake_redis, cid)

    assert await fake_redis.exists(debounce.claim_key(cid)) == 0


async def test_claim_key_expires_on_its_own_after_the_timeout(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0, timeout=0.05)
    assert await debounce.claim(fake_redis, cid, now=100.0) is False  # key still held

    await asyncio.sleep(0.1)  # the claimer died: nothing renews the key

    assert await fake_redis.exists(debounce.claim_key(cid)) == 0
    assert await debounce.claim(fake_redis, cid, now=100.0) is True


async def test_extend_claim_refreshes_the_claim_key(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0, timeout=0.2)

    await debounce.extend_claim(fake_redis, cid, now=10.0, timeout=30.0)

    assert await fake_redis.pttl(debounce.claim_key(cid)) > 1_000
    assert await fake_redis.zscore(DUE_KEY, str(cid)) == 40.0


async def test_extend_claim_does_not_create_a_claim_key(fake_redis):
    cid = uuid.uuid4()
    await debounce.schedule(fake_redis, cid, 1.0)
    await debounce.extend_claim(fake_redis, cid, now=10.0)
    assert await fake_redis.exists(debounce.claim_key(cid)) == 0


async def test_message_pushed_mid_turn_is_not_claimable_before_the_turn_ends(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=1.0)
    assert await debounce.claim(fake_redis, cid, now=2.0, timeout=60.0)
    await debounce.drain_pending(fake_redis, cid)

    # A message lands mid-turn with the usual short debounce window.
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=2.2)

    assert await fake_redis.zscore(DUE_KEY, str(cid)) == 62.0  # not pulled earlier
    assert await debounce.claim(fake_redis, cid, now=10.0) is False

    await debounce.finish(fake_redis, cid)  # the turn ends: due now, for the new message
    assert await debounce.claim(fake_redis, cid, now=time.time() + 1) is True


async def test_debounce_window_still_extends_with_each_message(fake_redis):
    cid = uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=5.0)
    await debounce.push_pending(fake_redis, cid, uuid.uuid4(), due_at=6.0)
    assert await fake_redis.zscore(DUE_KEY, str(cid)) == 6.0


async def test_return_inflight_puts_batch_ahead_of_newer(fake_redis):
    cid = uuid.uuid4()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for mid in (a, b):
        await debounce.push_pending(fake_redis, cid, mid, due_at=1.0)
    await debounce.drain_pending(fake_redis, cid)
    await debounce.push_pending(fake_redis, cid, c, due_at=1.0)

    await debounce.return_inflight(fake_redis, cid)

    assert _ids(await fake_redis.lrange(pending_key(cid), 0, -1)) == [a, b, c]
    assert await fake_redis.exists(debounce.inflight_key(cid)) == 0
    await debounce.return_inflight(fake_redis, cid)  # nothing in flight: no-op
    assert _ids(await fake_redis.lrange(pending_key(cid), 0, -1)) == [a, b, c]


async def test_push_during_return_inflight_keeps_order(fake_redis, monkeypatch):
    cid = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    await debounce.push_pending(fake_redis, cid, a, due_at=1.0)
    await debounce.drain_pending(fake_redis, cid)

    async def push_b():
        await debounce.push_pending(fake_redis, cid, b, due_at=1.0)

    fired = _interleave(monkeypatch, fake_redis, "lrange", push_b)
    await debounce.return_inflight(fake_redis, cid)

    assert fired
    assert _ids(await fake_redis.lrange(pending_key(cid), 0, -1)) == [a, b]
    assert await fake_redis.exists(debounce.inflight_key(cid)) == 0
