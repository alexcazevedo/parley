"""Redis-backed `Queue`, crash-safe.

`enqueue` LPUSHes onto `harness:jobs`; `dequeue` atomically BLMOVEs the job
into this worker's processing list (`harness:processing:{worker_id}`), so a
job is always in exactly one list. `ack`/`fail` remove it from there. Each
worker heartbeats an "alive" key with a TTL and registers itself in
`harness:workers`; `reap_dead_workers` moves the processing list of any
registered worker whose alive key is gone back to `harness:jobs`.

Delivery is at-least-once: a worker that is only slow may be reaped and its
job handled twice (the debounce batch dedupes message ids). Dead-lettered
jobs go to `harness:dlq`.
"""

import uuid
from typing import cast

import structlog
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.queue.base import Job, Queue

JOBS_KEY = "harness:jobs"
DLQ_KEY = "harness:dlq"
MAX_ATTEMPTS = 3
PROCESSING_KEY_PREFIX = "harness:processing:"
WORKERS_KEY = "harness:workers"
WORKER_ALIVE_PREFIX = "harness:worker:"
WORKER_TTL_SECONDS = 30

logger = structlog.get_logger(__name__)


class RedisQueue(Queue):
    def __init__(self, redis: Redis, worker_id: str | None = None) -> None:
        self.redis = redis
        self.worker_id = worker_id or uuid.uuid4().hex
        self._processing_key = f"{PROCESSING_KEY_PREFIX}{self.worker_id}"

    async def enqueue(self, job: Job) -> None:
        await self.redis.lpush(JOBS_KEY, job.to_json())

    async def heartbeat(self) -> None:
        """Register this worker and refresh its alive key (one MULTI/EXEC)."""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.sadd(WORKERS_KEY, self.worker_id)
            pipe.set(f"{WORKER_ALIVE_PREFIX}{self.worker_id}", "1", ex=WORKER_TTL_SECONDS)
            await pipe.execute()

    async def dequeue(self, timeout: int = 0) -> Job | None:
        # Re-register first: a worker wrongly reaped while slow becomes
        # visible again before it takes a new job.
        await self.heartbeat()
        try:
            raw = await self.redis.blmove(
                JOBS_KEY, self._processing_key, timeout, src="RIGHT", dest="LEFT"
            )
        except RedisTimeoutError:
            # Some client configurations surface an idle BLMOVE as a socket
            # timeout instead of a nil reply; treat it as "nothing to do".
            return None
        if raw is None:
            return None
        return Job.from_json(cast(str, raw))

    async def ack(self, job: Job) -> None:
        await self.redis.lrem(self._processing_key, 1, job.to_json())

    async def fail(self, job: Job) -> None:
        next_attempt = job.attempt + 1
        retried = Job(
            conversation_id=job.conversation_id,
            message_id=job.message_id,
            attempt=next_attempt,
        )
        target = DLQ_KEY if next_attempt >= MAX_ATTEMPTS else JOBS_KEY
        # One MULTI/EXEC: the job is never in neither list.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrem(self._processing_key, 1, job.to_json())
            pipe.lpush(target, retried.to_json())
            await pipe.execute()

    async def reap_dead_workers(self) -> int:
        """Requeue the processing lists of workers whose alive key expired.

        Each job moves with one atomic LMOVE (never in neither list; two
        reapers never move the same job). Processing lists hold newest at
        the left, so moving left -> right of `harness:jobs` puts the oldest
        job at the right end, where `dequeue` takes from.
        """
        total = 0
        for member in await self.redis.smembers(WORKERS_KEY):
            worker_id = member if isinstance(member, str) else member.decode()
            if worker_id == self.worker_id:
                continue
            alive_key = f"{WORKER_ALIVE_PREFIX}{worker_id}"
            if await self.redis.exists(alive_key):
                continue
            processing_key = f"{PROCESSING_KEY_PREFIX}{worker_id}"
            moved = 0
            while await self.redis.lmove(processing_key, JOBS_KEY, "LEFT", "RIGHT") is not None:
                moved += 1
            total += moved
            if moved:
                logger.info("worker.jobs_reaped", dead_worker_id=worker_id, count=moved)
            # Unregister only if it stayed dead and empty; otherwise leave it
            # registered so the next pass (or its own heartbeat) deals with it.
            if await self.redis.exists(alive_key):
                continue
            await self.redis.srem(WORKERS_KEY, worker_id)
            if await self.redis.llen(processing_key) > 0:
                await self.redis.sadd(WORKERS_KEY, worker_id)
        return total
