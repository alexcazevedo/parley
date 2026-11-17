"""Crash-safe queue: a job taken by a worker that dies is put back."""

import asyncio
import uuid

from app.conversations import debounce
from app.queue.base import Job
from app.queue.redis_queue import (
    DLQ_KEY,
    JOBS_KEY,
    PROCESSING_KEY_PREFIX,
    WORKER_ALIVE_PREFIX,
    WORKER_TTL_SECONDS,
    WORKERS_KEY,
    RedisQueue,
)
from app.workers.main import run_once


def _job() -> Job:
    return Job(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())


def _processing(worker_id: str) -> str:
    return f"{PROCESSING_KEY_PREFIX}{worker_id}"


async def _expire(redis, worker_id: str) -> None:
    await redis.delete(f"{WORKER_ALIVE_PREFIX}{worker_id}")


async def test_dequeued_unacked_job_is_in_processing_list_only(fake_redis):
    queue = RedisQueue(fake_redis, worker_id="w1")
    assert queue.worker_id == "w1"
    job = _job()
    await queue.enqueue(job)

    got = await queue.dequeue(timeout=1)

    assert got == job
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert await fake_redis.lrange(_processing("w1"), 0, -1) == [job.to_json()]


async def test_missing_worker_id_is_random():
    a = RedisQueue(None)  # type: ignore[arg-type]
    b = RedisQueue(None)  # type: ignore[arg-type]
    assert a.worker_id and b.worker_id and a.worker_id != b.worker_id


async def test_heartbeat_registers_worker_with_ttl(fake_redis):
    queue = RedisQueue(fake_redis, worker_id="w1")
    await queue.heartbeat()
    assert await fake_redis.sismember(WORKERS_KEY, "w1")
    ttl = await fake_redis.ttl(f"{WORKER_ALIVE_PREFIX}w1")
    assert 0 < ttl <= WORKER_TTL_SECONDS


async def test_acked_job_is_in_neither_list(fake_redis):
    queue = RedisQueue(fake_redis, worker_id="w1")
    await queue.enqueue(_job())
    job = await queue.dequeue(timeout=1)
    await queue.ack(job)
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert await fake_redis.llen(_processing("w1")) == 0


async def test_fail_removes_from_processing_and_retries_then_dead_letters(fake_redis):
    queue = RedisQueue(fake_redis, worker_id="w1")
    await queue.enqueue(_job())
    job = await queue.dequeue(timeout=1)
    await queue.fail(job)
    assert await fake_redis.llen(_processing("w1")) == 0
    assert await fake_redis.llen(JOBS_KEY) == 1

    dead = Job(job.conversation_id, job.message_id, attempt=2)
    await queue.enqueue(dead)
    await fake_redis.delete(JOBS_KEY)
    await fake_redis.lpush(JOBS_KEY, dead.to_json())
    got = await queue.dequeue(timeout=1)
    await queue.fail(got)
    assert await fake_redis.llen(_processing("w1")) == 0
    assert await fake_redis.llen(DLQ_KEY) == 1


async def test_dead_workers_job_is_reaped_and_processed(fake_redis):
    dying = RedisQueue(fake_redis, worker_id="dying")
    await dying.heartbeat()
    job = _job()
    await dying.enqueue(job)
    assert await dying.dequeue(timeout=1) == job
    # Worker dies here: no push_pending, no ack. Its alive key expires.
    await _expire(fake_redis, "dying")

    other = RedisQueue(fake_redis, worker_id="other")
    await other.heartbeat()
    assert await other.reap_dead_workers() == 1

    assert await fake_redis.llen(_processing("dying")) == 0
    assert not await fake_redis.sismember(WORKERS_KEY, "dying")
    assert await fake_redis.llen(JOBS_KEY) == 1

    got = await run_once(other, timeout=1, debounce_seconds=0.1)
    assert got == job
    assert await debounce.drain_pending(fake_redis, job.conversation_id) == [job.message_id]
    assert await fake_redis.llen(_processing("other")) == 0
    assert await fake_redis.llen(JOBS_KEY) == 0
    # Nothing left to reap.
    assert await other.reap_dead_workers() == 0


async def test_live_workers_jobs_are_not_reaped(fake_redis):
    live = RedisQueue(fake_redis, worker_id="live")
    await live.heartbeat()
    await live.enqueue(_job())
    await live.dequeue(timeout=1)

    other = RedisQueue(fake_redis, worker_id="other")
    await other.heartbeat()
    assert await other.reap_dead_workers() == 0
    assert await fake_redis.llen(_processing("live")) == 1
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_reaper_never_touches_its_own_list(fake_redis):
    me = RedisQueue(fake_redis, worker_id="me")
    await me.heartbeat()
    await me.enqueue(_job())
    await me.dequeue(timeout=1)
    await _expire(fake_redis, "me")
    assert await me.reap_dead_workers() == 0
    assert await fake_redis.llen(_processing("me")) == 1


async def test_reaped_jobs_are_dequeued_oldest_first(fake_redis):
    dying = RedisQueue(fake_redis, worker_id="dying")
    jobs = [_job() for _ in range(3)]
    for j in jobs:
        await dying.enqueue(j)
    for _ in jobs:
        await dying.dequeue(timeout=1)
    await _expire(fake_redis, "dying")

    other = RedisQueue(fake_redis, worker_id="other")
    assert await other.reap_dead_workers() == 3
    order = [await other.dequeue(timeout=1) for _ in jobs]
    assert order == jobs


async def test_two_reapers_move_each_job_once(fake_redis):
    dying = RedisQueue(fake_redis, worker_id="dying")
    for _ in range(5):
        await dying.enqueue(_job())
        await dying.dequeue(timeout=1)
    await _expire(fake_redis, "dying")

    a = RedisQueue(fake_redis, worker_id="a")
    b = RedisQueue(fake_redis, worker_id="b")
    counts = await asyncio.gather(a.reap_dead_workers(), b.reap_dead_workers())

    assert sum(counts) == 5
    assert await fake_redis.llen(JOBS_KEY) == 5


async def test_message_id_pushed_twice_is_drained_once(fake_redis):
    conversation_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    for mid in (first, second, first):
        await debounce.push_pending(fake_redis, conversation_id, mid, due_at=1.0)

    assert await debounce.drain_pending(fake_redis, conversation_id) == [first, second]
