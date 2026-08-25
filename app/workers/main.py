"""Worker process entrypoint.

Two concurrent halves:

- the job loop (`run_once`) dequeues jobs and pushes each message onto its
  conversation's pending list, re-arming the debounce window;
- the scheduler task (`app.workers.scheduler`) picks conversations up once
  that window closes and runs a single turn for the whole batch.

`run_once` is the unit-tested piece (works against fakeredis); `main` just
drives both forever against the real Redis/Postgres connections.
"""

import asyncio
import time

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.conversations import debounce
from app.conversations.turn import EchoRuntime, TurnRuntime

# The worker runs outside FastAPI, so it owns its own session factory rather
# than going through `app.api.deps.get_db`.
from app.db import async_session_maker
from app.observability.logging import configure_logging
from app.queue.base import Job, Queue
from app.queue.redis_queue import RedisQueue
from app.workers.scheduler import run_scheduler

configure_logging()
logger = structlog.get_logger(__name__)

DEQUEUE_TIMEOUT_SECONDS = 5


async def run_once(
    queue: Queue,
    timeout: int = DEQUEUE_TIMEOUT_SECONDS,
    *,
    redis: Redis | None = None,
    debounce_seconds: float | None = None,
) -> Job | None:
    """Dequeue a single job, add it to its conversation's debounce batch, ack.

    Returns the dequeued `Job`, or `None` if the queue was empty for the
    whole `timeout` window — so both the main loop and tests can drive this
    one step at a time. `redis` defaults to the queue's own client.
    """
    job = await queue.dequeue(timeout=timeout)
    if job is None:
        return None
    logger.info(
        "worker.job_received",
        conversation_id=str(job.conversation_id),
        message_id=str(job.message_id),
        attempt=job.attempt,
    )

    redis = redis if redis is not None else getattr(queue, "redis", None)
    if redis is not None:
        if debounce_seconds is None:
            debounce_seconds = get_settings().debounce_seconds
        # Always overwrite the due score: each new message extends the window.
        await debounce.push_pending(
            redis,
            job.conversation_id,
            job.message_id,
            due_at=time.time() + debounce_seconds,
        )

    await queue.ack(job)
    return job


async def main() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisQueue(redis)

    runtime: TurnRuntime = EchoRuntime()
    logger.info("worker.starting")
    scheduler_task = asyncio.create_task(run_scheduler(redis, async_session_maker, runtime))
    try:
        while True:
            await run_once(queue, redis=redis, debounce_seconds=settings.debounce_seconds)
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
