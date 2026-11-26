"""Worker process entrypoint.

Two concurrent halves:

- the job loop (`run_once`) dequeues jobs and pushes each message onto its
  conversation's pending list, re-arming the debounce window;
- the scheduler task (`app.workers.scheduler`) picks conversations up once
  that window closes and runs a single turn for the whole batch.

`run_once` is the unit-tested piece (works against fakeredis); `main` drives
both against the real Redis/Postgres connections until `SIGTERM` or `SIGINT`.

`job_loop` repeats `run_once` and survives errors: it logs
`worker.job_loop_error` and pauses `JOB_LOOP_ERROR_PAUSE_SECONDS`.

`event_push_loop` runs `app.events.delivery.deliver_due` every
`EVENT_PUSH_INTERVAL_SECONDS` in its own task (pushes of `external` channel
events); an error is logged as `worker.event_push_error` and the loop goes on.
`knowledge_ingest_loop` runs `app.knowledge.ingest.process_due` every
`KNOWLEDGE_INGEST_INTERVAL_SECONDS` the same way (`worker.knowledge_ingest_error`)
and is stopped like the push loop. `mcp_sync_loop` runs `app.mcp.sync.sync_due`
every `MCP_SYNC_POLL_SECONDS` (or `MCP_SYNC_INTERVAL_SECONDS` when shorter) the
same way (`worker.mcp_sync_error`); a pass syncs only the servers due.

Shutdown (PRODUCT.md E3.4): the signal sets `stop`. The job loop ends after
its current `run_once` (at most `DEQUEUE_TIMEOUT_SECONDS`) or at once if it is
in the error pause; the scheduler
claims nothing new and lets its running turns finish; the push loop ends
after its current pass (cancelled past `EVENT_PUSH_SHUTDOWN_SECONDS`; a
cancelled push is retried after its claim lease). `main` waits for it up
to `SHUTDOWN_GRACE_SECONDS`, then cancels what is left: those turns release
their locks, and their batches come back through the claim timeout. Last, the
shared HTTP client and the Redis connection are closed.
"""

import asyncio
import signal
import time
from collections.abc import Callable
from typing import Any

import structlog
from redis.asyncio import Redis

from app.agents.boundaries import load_floor
from app.agents.runtime import AgentRuntime
from app.config import assert_secure_settings, get_settings
from app.conversations import debounce
from app.conversations.turn import AnyTurnRuntime

# The worker runs outside FastAPI, so it owns its own session factory rather
# than going through `app.api.deps.get_db`.
from app.db import async_session_maker
from app.events.delivery import SessionFactory, deliver_due
from app.http_client import close_shared_client, shared_client
from app.knowledge.ingest import process_due
from app.mcp.sync import sync_due
from app.observability.logging import configure_logging
from app.queue.base import Job, Queue
from app.queue.redis_queue import WORKER_TTL_SECONDS, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.scheduler import run_scheduler

configure_logging()
logger = structlog.get_logger(__name__)

DEQUEUE_TIMEOUT_SECONDS = 5
# How long a stopping worker waits for its running turns before cancelling them.
SHUTDOWN_GRACE_SECONDS = 90
# Wait after an error in the job loop (Redis down, a failing job) before the
# next try, so a dead Redis is not hit in a tight loop.
JOB_LOOP_ERROR_PAUSE_SECONDS = 1.0
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# How long a stopping worker waits for the push loop's current pass.
EVENT_PUSH_SHUTDOWN_SECONDS = 10.0
# How often the worker looks for MCP servers due for a sync.
MCP_SYNC_POLL_SECONDS = 30.0


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


async def job_loop(
    queue: Queue,
    redis: Redis,
    stop: asyncio.Event,
    debounce_seconds: float,
) -> None:
    """Run `run_once` until `stop` is set; an error never ends the loop.

    An ordinary exception (Redis down, a job that cannot be handled) is logged
    as `worker.job_loop_error`, then the loop waits
    `JOB_LOOP_ERROR_PAUSE_SECONDS` (cut short by `stop`) and tries again.
    Cancellation is not caught.
    """
    while not stop.is_set():
        try:
            await run_once(queue, redis=redis, debounce_seconds=debounce_seconds)
        except Exception:
            logger.exception("worker.job_loop_error")
            try:
                async with asyncio.timeout(JOB_LOOP_ERROR_PAUSE_SECONDS):
                    await stop.wait()
            except TimeoutError:
                pass


async def housekeeping_loop(
    queue: RedisQueue, interval: float = WORKER_TTL_SECONDS / 3
) -> None:
    """Heartbeat and reap dead workers now, then every `interval` seconds.

    A Redis (or any) error is logged and retried on the next beat; only
    cancellation stops the loop.
    """
    while True:
        try:
            await queue.heartbeat()
            await queue.reap_dead_workers()
        except Exception:
            logger.exception("worker.housekeeping_failed")
        await asyncio.sleep(interval)


async def event_push_loop(
    session_factory: SessionFactory, stop: asyncio.Event, interval: float
) -> None:
    """Run `deliver_due` every `interval` seconds until `stop` is set.

    An error is logged as `worker.event_push_error`; the next pass comes after
    the same interval. The wait is cut short by `stop`.
    """
    while not stop.is_set():
        try:
            await deliver_due(session_factory, shared_client())
        except Exception:
            logger.exception("worker.event_push_error")
        try:
            async with asyncio.timeout(interval):
                await stop.wait()
        except TimeoutError:
            pass


async def knowledge_ingest_loop(
    session_factory: SessionFactory, stop: asyncio.Event, interval: float
) -> None:
    """Run `process_due` every `interval` seconds until `stop` is set.

    An error is logged as `worker.knowledge_ingest_error`; the next pass comes
    after the same interval. The wait is cut short by `stop`.
    """
    while not stop.is_set():
        try:
            await process_due(session_factory)
        except Exception:
            logger.exception("worker.knowledge_ingest_error")
        try:
            async with asyncio.timeout(interval):
                await stop.wait()
        except TimeoutError:
            pass


async def mcp_sync_loop(
    session_factory: SessionFactory, stop: asyncio.Event, interval: float
) -> None:
    """Run `sync_due` every `interval` seconds until `stop` is set.

    An error is logged as `worker.mcp_sync_error`; the next pass comes after
    the same interval. The wait is cut short by `stop`.
    """
    while not stop.is_set():
        try:
            await sync_due(session_factory)
        except Exception:
            logger.exception("worker.mcp_sync_error")
        try:
            async with asyncio.timeout(interval):
                await stop.wait()
        except TimeoutError:
            pass


async def _stop_event_push(
    task: asyncio.Task, failed_event: str = "worker.event_push_failed"
) -> None:
    """Wait for the push loop's (or the ingest loop's) current pass, then
    cancel it if still running."""
    done, _ = await asyncio.wait({task}, timeout=EVENT_PUSH_SHUTDOWN_SECONDS)
    if not done:
        task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    if isinstance(results[0], Exception):
        logger.error(failed_event, exc_info=results[0])


def _install_stop_signals(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> Callable[[], None]:
    """Make `SIGTERM`/`SIGINT` set `stop`; return a function that undoes it.

    Where the loop cannot take signal handlers (Windows, or not the main
    thread) nothing is installed: Ctrl-C then cancels `main`, and its cleanup
    still runs the same shutdown.
    """
    installed: list[tuple[signal.Signals, Any]] = []
    for sig in _STOP_SIGNALS:
        previous = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, _request_stop, stop, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.warning("worker.signal_handler_unavailable", signal=sig.name)
            continue
        installed.append((sig, previous))

    def restore() -> None:
        for sig, previous in installed:
            loop.remove_signal_handler(sig)
            if previous is not None:
                signal.signal(sig, previous)

    return restore


def _request_stop(stop: asyncio.Event, sig: signal.Signals) -> None:
    if not stop.is_set():
        logger.info("worker.stopping", signal=sig.name)
    stop.set()


async def _stop_scheduler(scheduler_task: asyncio.Task) -> None:
    """Wait for the scheduler up to the grace period, then cancel it."""
    done, _ = await asyncio.wait({scheduler_task}, timeout=SHUTDOWN_GRACE_SECONDS)
    if not done:
        logger.warning("worker.shutdown_grace_expired", grace_seconds=SHUTDOWN_GRACE_SECONDS)
        scheduler_task.cancel()
    results = await asyncio.gather(scheduler_task, return_exceptions=True)
    if isinstance(results[0], Exception):
        logger.error("worker.scheduler_failed", exc_info=results[0])


async def _close_connections(redis: Redis) -> None:
    """Close the shared HTTP client, then Redis (Redis even if the client fails)."""
    try:
        await close_shared_client()
    finally:
        await redis.aclose()


async def main() -> None:
    settings = get_settings()
    assert_secure_settings(settings)
    load_floor(settings.boundary_floor_file)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisQueue(redis)

    # The real agent loop. `ToolRegistry()` uses the default executors (built-ins
    # first, then the HTTP executor); `ToolRegistry([...])` takes them in
    # priority order.
    runtime: AnyTurnRuntime = AgentRuntime(registry=ToolRegistry(), settings=settings)
    stop = asyncio.Event()
    restore_signals = _install_stop_signals(asyncio.get_running_loop(), stop)
    logger.info("worker.starting", concurrency=settings.worker_concurrency)
    scheduler_task = asyncio.create_task(
        run_scheduler(
            redis,
            async_session_maker,
            runtime,
            concurrency=settings.worker_concurrency,
            stop=stop,
        )
    )
    housekeeping_task = asyncio.create_task(housekeeping_loop(queue))
    push_task = asyncio.create_task(
        event_push_loop(async_session_maker, stop, settings.event_push_interval_seconds)
    )
    ingest_task = asyncio.create_task(
        knowledge_ingest_loop(
            async_session_maker, stop, settings.knowledge_ingest_interval_seconds
        )
    )
    mcp_task = asyncio.create_task(
        mcp_sync_loop(
            async_session_maker,
            stop,
            min(settings.mcp_sync_interval_seconds, MCP_SYNC_POLL_SECONDS),
        )
    )
    try:
        await job_loop(queue, redis, stop, settings.debounce_seconds)
    finally:
        stop.set()
        try:
            await asyncio.gather(
                _stop_scheduler(scheduler_task),
                _stop_event_push(push_task),
                # Same bounded wait as the push loop: an item cut off is taken
                # again after its claim lease.
                _stop_event_push(ingest_task, "worker.knowledge_ingest_failed"),
                # A sync cut off is only a discovery; the next pass repeats it.
                _stop_event_push(mcp_task, "worker.mcp_sync_failed"),
            )
        finally:
            housekeeping_task.cancel()
            await asyncio.gather(housekeeping_task, return_exceptions=True)
            restore_signals()
            await _close_connections(redis)
            logger.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
