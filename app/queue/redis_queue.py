"""Redis-backed `Queue`: a simple `LPUSH`/`BRPOP` list, with a dead-letter
list for jobs that exhaust their retries.
"""

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.queue.base import Job, Queue

JOBS_KEY = "harness:jobs"
DLQ_KEY = "harness:dlq"
MAX_ATTEMPTS = 3


class RedisQueue(Queue):
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def enqueue(self, job: Job) -> None:
        await self.redis.lpush(JOBS_KEY, job.to_json())

    async def dequeue(self, timeout: int = 0) -> Job | None:
        try:
            result = await self.redis.brpop([JOBS_KEY], timeout=timeout)
        except RedisTimeoutError:
            # Some client configurations surface an idle BRPOP as a socket
            # timeout instead of a nil reply; treat it as "nothing to do".
            return None
        if result is None:
            return None
        _, raw = result
        return Job.from_json(raw)

    async def ack(self, job: Job) -> None:
        # BRPOP already removed the job from the list; nothing left to do.
        return None

    async def fail(self, job: Job) -> None:
        next_attempt = job.attempt + 1
        retried = Job(
            conversation_id=job.conversation_id,
            message_id=job.message_id,
            attempt=next_attempt,
        )
        if next_attempt >= MAX_ATTEMPTS:
            await self.redis.lpush(DLQ_KEY, retried.to_json())
            return
        await self.redis.lpush(JOBS_KEY, retried.to_json())
