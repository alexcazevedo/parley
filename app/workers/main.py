"""Worker process entrypoint.

Dequeues jobs from the Redis queue, logs them, and acks. `run_once` is the
unit tested piece (works against fakeredis); the module-level `main` loop
just drives it forever against the real Redis connection.

TODO(ticket 04+): replace the log-and-ack body with per-conversation
debounce/lock and turn execution.
"""

import asyncio

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.observability.logging import configure_logging
from app.queue.base import Job, Queue
from app.queue.redis_queue import RedisQueue

configure_logging()
logger = structlog.get_logger(__name__)

DEQUEUE_TIMEOUT_SECONDS = 5


async def run_once(queue: Queue, timeout: int = DEQUEUE_TIMEOUT_SECONDS) -> Job | None:
    """Dequeue a single job, log it, and ack it.

    Returns the dequeued `Job`, or `None` if the queue was empty for the
    whole `timeout` window — so both the main loop and tests can drive this
    one step at a time.
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
    await queue.ack(job)
    return job


async def main() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisQueue(redis)
    logger.info("worker.starting")
    while True:
        await run_once(queue)


if __name__ == "__main__":
    asyncio.run(main())
